"""Explicit lifecycle wrapper for a configured Ray Host connection."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import ray

SUPPORTED_RAY_VERSION = "2.55.1"


def validate_ray_version() -> None:
    if ray.__version__ != SUPPORTED_RAY_VERSION:
        raise RuntimeError(
            "Ray Host correctness requires "
            f"ray=={SUPPORTED_RAY_VERSION}; found {ray.__version__}"
        )


def current_ray_node_id() -> str:
    if not ray.is_initialized():
        raise RuntimeError("Ray is not initialized")
    return str(ray.get_runtime_context().get_node_id())


def current_ray_address() -> str:
    if not ray.is_initialized():
        raise RuntimeError("Ray is not initialized")
    return str(ray.get_runtime_context().gcs_address)


@dataclass(frozen=True, slots=True)
class RayClusterConfig:
    namespace: str
    address: str | None = None
    temp_directory: str | None = None
    include_dashboard: bool = False
    local_num_cpus: int | None = None
    local_object_store_memory: int | None = None
    disable_ray_npu_resource: bool = False

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("Ray namespace is required")
        for name in ("local_num_cpus", "local_object_store_memory"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if self.temp_directory is not None and not self.temp_directory:
            raise ValueError("temp_directory cannot be empty")


class ManagedRayCluster:
    def __init__(self, config: RayClusterConfig) -> None:
        self.config = config
        self._started = False
        self._previous_accelerator_override: str | None = None
        self._accelerator_override_was_set = False

    def start(self) -> Any:
        if self._started:
            return ray.get_runtime_context()
        if ray.is_initialized():
            raise RuntimeError("Ray is already initialized outside ManagedRayCluster")
        validate_ray_version()
        if self.config.disable_ray_npu_resource:
            name = "RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO"
            self._accelerator_override_was_set = name in os.environ
            self._previous_accelerator_override = os.environ.get(name)
            os.environ[name] = "0"
        kwargs: dict[str, object] = {
            "namespace": self.config.namespace,
            "include_dashboard": self.config.include_dashboard,
        }
        if self.config.address is not None:
            kwargs["address"] = self.config.address
        else:
            kwargs["address"] = "local"
            if self.config.temp_directory is not None:
                kwargs["_temp_dir"] = self.config.temp_directory
            if self.config.local_num_cpus is not None:
                kwargs["num_cpus"] = self.config.local_num_cpus
            if self.config.local_object_store_memory is not None:
                kwargs["object_store_memory"] = self.config.local_object_store_memory
            if self.config.disable_ray_npu_resource:
                kwargs["resources"] = {"NPU": 0}
        ray_init: Any = ray.init
        try:
            context = ray_init(**kwargs)
        except BaseException:
            self._restore_accelerator_override()
            raise
        self._started = True
        return context

    def close(self) -> None:
        if not self._started:
            return
        try:
            ray.shutdown()
        finally:
            self._started = False
            self._restore_accelerator_override()

    def _restore_accelerator_override(self) -> None:
        if self.config.disable_ray_npu_resource:
            name = "RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO"
            if self._accelerator_override_was_set:
                assert self._previous_accelerator_override is not None
                os.environ[name] = self._previous_accelerator_override
            else:
                os.environ.pop(name, None)
            self._previous_accelerator_override = None
            self._accelerator_override_was_set = False

    def live_node_ids(self) -> tuple[str, ...]:
        if not self._started:
            raise RuntimeError("Ray cluster is not started")
        return tuple(
            sorted(
                str(node["NodeID"])
                for node in ray.nodes()
                if bool(node.get("Alive"))
            )
        )


class ManagedRayWorkerNode:
    """Own one public `ray start --block` worker generation by process group."""

    def __init__(
        self,
        *,
        address: str,
        namespace: str,
        node_ip: str,
        temp_directory: str,
        num_cpus: int,
        log_path: str | Path,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        if not address or not namespace or not node_ip or not temp_directory:
            raise ValueError("Ray worker bootstrap identities are required")
        if num_cpus < 1 or startup_timeout_seconds <= 0:
            raise ValueError("Ray worker capacity/deadline is invalid")
        self.address = address
        self.namespace = namespace
        self.node_ip = node_ip
        self.temp_directory = str(Path(temp_directory).resolve(strict=False))
        self.num_cpus = num_cpus
        self.log_path = Path(log_path).resolve(strict=False)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self.node_id: str | None = None
        self._log_stream: object | None = None

    def start(self) -> str:
        if self.process is not None:
            assert self.node_id is not None
            return self.node_id
        if ray.is_initialized():
            raise RuntimeError("Ray is already initialized outside ManagedRayWorkerNode")
        validate_ray_version()
        Path(self.temp_directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_stream = self.log_path.open("ab", buffering=0)
        self._log_stream = log_stream
        environment = dict(os.environ)
        environment["RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO"] = "0"
        ray_cli = Path(sys.executable).with_name("ray")
        if not ray_cli.is_file():
            raise RuntimeError(f"Ray CLI is unavailable beside Python: {ray_cli}")
        command = (
            str(ray_cli),
            "start",
            f"--address={self.address}",
            f"--node-ip-address={self.node_ip}",
            f"--num-cpus={self.num_cpus}",
            "--num-gpus=0",
            f"--resources={json.dumps({'NPU': 0}, separators=(',', ':'))}",
            f"--temp-dir={self.temp_directory}",
            "--disable-usage-stats",
            "--block",
            "--log-style=record",
            "--log-color=false",
        )
        deadline = time.monotonic() + self.startup_timeout_seconds
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            self.process = process
            # A remote driver can block while Ray tries to attach it to a
            # raylet. Start this node's raylet first so the bootstrap driver
            # has a local node when it connects to the GCS.
            if process.poll() is not None:
                raise RuntimeError(
                    f"managed Ray worker exited with code {process.returncode}"
                )
            ray.init(address=self.address, namespace=self.namespace)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"managed Ray worker exited with code {process.returncode}"
                    )
                matches = tuple(
                    str(item["NodeID"])
                    for item in ray.nodes()
                    if bool(item.get("Alive"))
                    and str(item.get("NodeManagerAddress")) == self.node_ip
                )
                if len(matches) == 1:
                    self.node_id = matches[0]
                    return self.node_id
                if len(matches) > 1:
                    raise RuntimeError(
                        "managed Ray worker join is ambiguous for node IP "
                        f"{self.node_ip}"
                    )
                time.sleep(0.1)
            raise TimeoutError("managed Ray worker did not join before deadline")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if ray.is_initialized():
            ray.shutdown()
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        stream = self._log_stream
        self._log_stream = None
        if stream is not None:
            stream.close()  # type: ignore[attr-defined]
        self.node_id = None
