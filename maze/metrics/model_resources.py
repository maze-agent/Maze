"""Resource sampling for a model process tree over individual task executions."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import psutil

logger = logging.getLogger(__name__)


class ModelProcessReader:
    """Read the model process tree without initializing CUDA in the observer."""

    def __init__(self, pid: int, gpu_id: int | None):
        self.process = psutil.Process(pid)
        self.started = time.time()
        self.previous = {}
        self.totals = {}
        self.nvml = None
        self.handle = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self.nvml = pynvml
            if gpu_id is not None:
                self.handle = pynvml.nvmlDeviceGetHandleByIndex(int(gpu_id))
        except Exception:
            logger.debug("NVML sampling unavailable", exc_info=True)

    def __call__(self) -> dict:
        values = {}
        pids = set()
        try:
            processes = [self.process, *self.process.children(recursive=True)]
        except psutil.Error:
            processes = [self.process]
        rss = 0
        memory_available = False
        alive = set()
        for process in processes:
            try:
                identity = (process.pid, process.create_time())
                alive.add(identity)
                pids.add(process.pid)
                with process.oneshot():
                    rss += process.memory_info().rss
                    memory_available = True
                    cpu = process.cpu_times()
                    counters = {"cpu_time_seconds": cpu.user + cpu.system}
                    try:
                        io = process.io_counters()
                        counters.update(
                            process_io_read_bytes=io.read_bytes,
                            process_io_write_bytes=io.write_bytes,
                        )
                    except (psutil.Error, AttributeError):
                        pass
                previous = self.previous.get(identity, {})
                for key, value in counters.items():
                    baseline = previous.get(
                        key, 0 if identity[1] >= self.started else value
                    )
                    self.totals[key] = self.totals.get(key, 0) + max(
                        0, value - baseline
                    )
                self.previous[identity] = counters
            except psutil.Error:
                continue
        self.previous = {
            key: value for key, value in self.previous.items() if key in alive
        }
        values.update(self.totals)
        if memory_available:
            values["process_rss_bytes"] = rss
        if self.nvml is not None and self.handle is not None:
            try:
                processes = self.nvml.nvmlDeviceGetComputeRunningProcesses(self.handle)
                amounts = [
                    int(p.usedGpuMemory)
                    for p in processes
                    if p.pid in pids and p.usedGpuMemory not in (None, (1 << 64) - 1)
                ]
                values["gpu_memory_observed_bytes"] = sum(amounts)
            except self.nvml.NVMLError:
                pass
            try:
                values["gpu_utilization_percent"] = (
                    self.nvml.nvmlDeviceGetUtilizationRates(self.handle).gpu
                )
            except self.nvml.NVMLError:
                pass
        return values

    def close(self):
        if self.nvml is not None:
            try:
                self.nvml.nvmlShutdown()
            except self.nvml.NVMLError:
                pass


class _ExecutionWindow:
    PEAKS = {
        "process_rss_bytes": "process_rss_peak_bytes",
        "gpu_memory_observed_bytes": "gpu_memory_peak_observed_bytes",
        "gpu_utilization_percent": "gpu_utilization_peak_percent",
    }
    COUNTERS = ("cpu_time_seconds", "process_io_read_bytes", "process_io_write_bytes")

    def __init__(self, timestamp: float, values: dict, interval: float):
        self.started = timestamp
        self.last_time = timestamp
        self.baseline = dict(values)
        self.last_values = dict(values)
        self.interval = interval
        self.samples = 0
        self.peaks = {}
        self.concurrent = 1

    def update(self, timestamp: float, values: dict, concurrent: int):
        elapsed = max(0, timestamp - self.last_time)
        if (
            elapsed >= self.interval / 2
            and "cpu_time_seconds" in values
            and "cpu_time_seconds" in self.last_values
        ):
            cores = (
                max(
                    0, values["cpu_time_seconds"] - self.last_values["cpu_time_seconds"]
                )
                / elapsed
            )
            self.peaks["cpu_peak_cores"] = max(
                self.peaks.get("cpu_peak_cores", 0), cores
            )
        for key, peak_key in self.PEAKS.items():
            if key in values:
                self.peaks[peak_key] = max(self.peaks.get(peak_key, 0), values[key])
        self.concurrent = max(self.concurrent, concurrent)
        self.last_values = dict(values)
        self.last_time = timestamp
        self.samples += 1

    def snapshot(self, status: str) -> dict:
        duration = max(0, self.last_time - self.started)
        result = {
            "memory_scope": "model_instance",
            "memory_source": "nvml_process_tree",
            "measurement_phase": "task_execution",
            "measurement_status": status,
            "started_at": self.started,
            "observed_at": self.last_time,
            "duration_seconds": duration,
            "sample_interval_seconds": self.interval,
            "sample_count": self.samples,
            "max_concurrent_observations": self.concurrent,
            **self.peaks,
        }
        if status == "completed":
            result["finished_at"] = self.last_time
        for key in self.COUNTERS:
            if key in self.baseline and key in self.last_values:
                result[key] = max(0, self.last_values[key] - self.baseline[key])
        if "cpu_time_seconds" in result and duration > 0:
            result["cpu_average_cores"] = result["cpu_time_seconds"] / duration
            result["cpu_peak_cores"] = max(
                result.get("cpu_peak_cores", 0), result["cpu_average_cores"]
            )
        if "gpu_utilization_peak_percent" in result:
            result["gpu_utilization_scope"] = "device"
        return result


class ModelResourceMonitor:
    """A bounded set of execution windows driven by one sampling thread per model."""

    def __init__(
        self,
        pid: int | None = None,
        gpu_id: int | None = None,
        *,
        interval: float = 0.05,
        reader=None,
        clock=time.time,
        max_windows=128,
    ):
        self.interval = interval
        self.reader = reader if reader is not None else ModelProcessReader(pid, gpu_id)
        self.clock = clock
        self.max_windows = max_windows
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._active = threading.Event()
        self._thread = None
        self._windows = {}
        self._retired = deque(maxlen=256)
        self._current = {}
        self._observed_at = None

    def _sample(self):
        values = self.reader()
        now = self.clock()
        self._current, self._observed_at = values, now
        for window in self._windows.values():
            window.update(now, values, len(self._windows))
        return now, values

    def _run(self):
        while not self._stop.is_set():
            self._active.wait()
            if self._stop.wait(self.interval):
                return
            try:
                with self._lock:
                    if self._windows:
                        self._sample()
            except Exception:
                logger.debug("Model resource sample unavailable", exc_info=True)

    def start(self):
        with self._lock:
            self._sample()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def begin(self, observation_id: str):
        with self._lock:
            if observation_id in self._retired:
                raise RuntimeError("Model resource observation already ended")
            if observation_id in self._windows:
                return self._windows[observation_id].snapshot("running")
            if len(self._windows) >= self.max_windows:
                raise RuntimeError("Model resource observation capacity reached")
            now, values = self._sample()
            window = _ExecutionWindow(now, values, self.interval)
            self._windows[observation_id] = window
            self._active.set()
            for active in self._windows.values():
                active.concurrent = max(active.concurrent, len(self._windows))
            window.update(now, values, len(self._windows))
            return window.snapshot("running")

    def end(self, observation_id: str):
        with self._lock:
            self._sample()
            window = self._windows.pop(observation_id, None)
            self._retired.append(observation_id)
            if not self._windows:
                self._active.clear()
            return window.snapshot("completed") if window is not None else {}

    def discard(self, observation_id: str):
        with self._lock:
            self._windows.pop(observation_id, None)
            self._retired.append(observation_id)
            if not self._windows:
                self._active.clear()

    def current(self):
        with self._lock:
            self._sample()
            return {**self._current, "observed_at": self._observed_at}

    def stop(self):
        self._stop.set()
        self._active.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                return
        with self._lock:
            self._windows.clear()
        close = getattr(self.reader, "close", None)
        if close is not None:
            close()


class RemoteModelObservation:
    """Best-effort model sampling around task code; failures never fail user work."""

    def __init__(self, observer: dict | None):
        self.observer = observer
        self.metrics = {}
        self.started = False

    def __enter__(self):
        if self.observer is not None:
            import ray

            try:
                ray.get(
                    self.observer["actor"].begin_resource_observation.remote(
                        self.observer["observation_id"]
                    ),
                    timeout=3,
                )
                self.started = True
            except Exception:
                logger.debug("Model observation could not start", exc_info=True)
        return self

    def __exit__(self, *exc):
        if self.started:
            import ray

            try:
                metrics = ray.get(
                    self.observer["actor"].end_resource_observation.remote(
                        self.observer["observation_id"]
                    ),
                    timeout=3,
                )
                if metrics:
                    self.metrics = {
                        **{k: v for k, v in self.observer.items() if k != "actor"},
                        **metrics,
                    }
            except Exception:
                logger.debug("Model observation could not finish", exc_info=True)
                try:
                    self.observer["actor"].discard_resource_observation.remote(
                        self.observer["observation_id"]
                    )
                except Exception:
                    pass
