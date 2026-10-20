"""Per-attempt worker process measurements, independent of resource reservations."""

import threading
import time

import psutil


class ProcessResourceSampler:
    """CPU/I/O counter deltas and sampled RSS for the executing worker process."""

    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.process = psutil.Process()
        self.metrics = {}
        self._stop = threading.Event()
        self._peak_rss = None
        self._samples = 0
        self._peak_cpu_cores = None
        self._last_cpu = None
        self._last_cpu_time = None

    def _read(self, method):
        try:
            return getattr(self.process, method)()
        except (psutil.Error, OSError, AttributeError):
            return None

    def _sample(self):
        memory = self._read("memory_info")
        if memory is not None:
            self._peak_rss = max(self._peak_rss or 0, memory.rss)
            self._samples += 1
        cpu = self._read("cpu_times")
        now = time.monotonic()
        if cpu is not None:
            total = cpu.user + cpu.system
            if (
                self._last_cpu is not None
                and now - self._last_cpu_time >= self.interval / 2
            ):
                cores = max(0, total - self._last_cpu) / (now - self._last_cpu_time)
                self._peak_cpu_cores = max(self._peak_cpu_cores or 0, cores)
            self._last_cpu, self._last_cpu_time = total, now

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def __enter__(self):
        self._started = time.monotonic()
        self._cpu = self._read("cpu_times")
        self._io = self._read("io_counters")
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self._sample()
        elapsed = max(time.monotonic() - self._started, 1e-9)
        cpu, io = self._read("cpu_times"), self._read("io_counters")
        self.metrics = {
            "process_resource_scope": "worker_process",
            "process_resource_sample_interval_seconds": self.interval,
            "process_resource_samples": self._samples,
            "process_resource_elapsed_seconds": elapsed,
        }
        if self._peak_rss is not None:
            self.metrics["process_rss_peak_bytes"] = self._peak_rss
        if self._peak_cpu_cores is not None:
            self.metrics["cpu_peak_cores"] = self._peak_cpu_cores
        if cpu is not None and self._cpu is not None:
            user = max(0.0, cpu.user - self._cpu.user)
            system = max(0.0, cpu.system - self._cpu.system)
            self.metrics.update(
                cpu_time_seconds=user + system,
                cpu_average_cores=(user + system) / elapsed,
                cpu_peak_cores=max(
                    self._peak_cpu_cores or 0, (user + system) / elapsed
                ),
            )
        if io is not None and self._io is not None:
            self.metrics.update(
                process_io_read_bytes=max(0, io.read_bytes - self._io.read_bytes),
                process_io_write_bytes=max(0, io.write_bytes - self._io.write_bytes),
            )
