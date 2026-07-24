#!/usr/bin/env python3
"""Generate private runtime configuration for the logical control plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


NODE_COUNT = 8
CONTROLLER_IP = "172.30.240.10"
NODE_RPC_PORT = 7124
TEXT_MODEL_PATH = Path("/home/user2/workplace/model_weight/model_from_hf/Qwen3-4B")
VISION_MODEL_PATH = Path(
    "/home/user2/workplace/model_weight/model_from_hf/Qwen2.5-VL-3B-Instruct"
)


def _write_private(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if isinstance(content, str):
        path.write_text(content, encoding="ascii")
    else:
        path.write_bytes(content)
    os.chmod(path, 0o600)


def _controller_config(profile: str = "correctness") -> str:
    if profile not in {"correctness", "performance"}:
        raise ValueError(f"unsupported logical-cluster profile: {profile}")
    if profile == "performance":
        recovery_name = "controller-transformers-performance-v3.sqlite3"
        scheduler_policy = "hacs_no_tp"
        anchor_strategy = "static"
        task_slots_total = 2
        allow_colocation = "true"
        # The current C10 contract has one global reuse limit. Colocated NPU
        # workers require one Attempt per process, so CPU/I/O-only reuse cannot
        # be enabled independently yet.
        max_tasks_per_worker = 1
        standby_min_idle = 1
        standby_max_idle = 2
    else:
        recovery_name = "controller-transformers-correctness.sqlite3"
        scheduler_policy = "fcfs"
        anchor_strategy = "declared_only"
        task_slots_total = 1
        allow_colocation = "false"
        max_tasks_per_worker = 1
        standby_min_idle = 0
        standby_max_idle = 0
    return f"""schema_version = 1
profile = "{profile}"

[control]
socket_path = "/workspace/state/control-plane/control.sock"
runtime_directory = "/workspace/state/control-plane"
pid_file = "/workspace/state/control-plane/controller.pid"
cluster_token_file = "/workspace/state/control-plane/cluster.token"
recovery_path = "/workspace/state/control-plane/{recovery_name}"
node_rpc_bind_address = "0.0.0.0:{NODE_RPC_PORT}"
node_rpc_advertised_host = "{CONTROLLER_IP}"

[cluster]
cluster_id = "ascend-maze-logical"
environment_fingerprint = "auto"
expected_node_count = {NODE_COUNT}
head_node_id = "node-0"
head_node_ip = "{CONTROLLER_IP}"

[runtime.ray]
namespace = "ascend-maze-logical"
temp_directory = "/workspace/state/control-plane/ray-head"
object_store_memory_bytes = 8589934592
include_dashboard = false
local_num_cpus = 20
disable_ray_npu_resource = true

[scheduler]
policy = "{scheduler_policy}"
partitioner = "heterogeneous"
dispatch_timeout_ms = 60000

[placement]
anchor_strategy = "{anchor_strategy}"
task_slots_total = {task_slots_total}
allow_colocation = {allow_colocation}
npu_system_reserved_hbm_mb = 4096
npu_hbm_headroom_mb = 1024
host_mem_headroom_mb = 1024
io_slots_total = 8

[worker]
max_tasks_per_worker = {max_tasks_per_worker}
standby_min_idle = {standby_min_idle}
standby_max_idle = {standby_max_idle}
max_total = 64

[inference]
model_catalog_path = "/workspace/state/control-plane/model_catalog.toml"
reconcile_interval_ms = 100

[recording]
backend = "noop"
root_directory = "/workspace/state/control-plane/records"
"""


def _artifact_revision(path: Path) -> str:
    digests: list[tuple[str, str]] = []
    for name in (
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "tokenizer_config.json",
    ):
        candidate = path / name
        if candidate.is_file():
            digests.append((name, hashlib.sha256(candidate.read_bytes()).hexdigest()))
    if not digests:
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return hashlib.sha256(
        json.dumps(digests, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _model_catalog(profile: str = "correctness") -> str:
    if profile not in {"correctness", "performance"}:
        raise ValueError(f"unsupported logical-cluster profile: {profile}")
    text_revision = _artifact_revision(TEXT_MODEL_PATH)
    vision_revision = _artifact_revision(VISION_MODEL_PATH)
    max_replicas = 8 if profile == "performance" else 1
    max_parallel_starts = max_replicas
    scale_cooldown_ms = 0 if profile == "performance" else 600_000
    scale_down_idle_ms = 0 if profile == "performance" else 600_000
    catalog_profile = "performance-v3" if profile == "performance" else profile
    return f'''schema_version = 1
catalog_revision = "logical-{catalog_profile}-{text_revision[:12]}-{vision_revision[:12]}"

[[models]]
model_id = "qwen3-4b-e2e"
artifact_path = "{TEXT_MODEL_PATH}"
tokenizer_path = "{TEXT_MODEL_PATH}"
artifact_revision = "{text_revision}"
backend = "transformers_local"
dtype = "bfloat16"
tensor_parallel_size = 1
max_model_len = 10240
instance_cpu_num = 4
instance_host_mem_mb = 16384
weight_hbm_mb = 8192
runtime_hbm_mb = 4096
kv_cache_hbm_mb = 1536
instance_hbm_mb = 13824
npu_slots = 1
allow_colocation = true
request_capacity = 1
required_capabilities = ["transformers_local"]
min_replicas = 0
max_replicas = {max_replicas}
target_route_utilization = 1.0
scale_up_pending_threshold = 1
scale_up_sustain_ms = 0
scale_down_idle_ms = {scale_down_idle_ms}
scale_cooldown_ms = {scale_cooldown_ms}
max_parallel_starts = {max_parallel_starts}
startup_timeout_ms = 600000
drain_timeout_ms = 120000

[models.launch_options]
enable_thinking = false
generation_method = "manual_greedy"
model_kind = "text"
request_timeout_ms = 600000
trust_remote_code = true

[models.warmup_request]
messages = [{{ role = "user", content = "Reply with exactly: ready" }}]
max_tokens = 8
temperature = 0.0

[[models]]
model_id = "qwen2_5-vl-3b-e2e"
artifact_path = "{VISION_MODEL_PATH}"
tokenizer_path = "{VISION_MODEL_PATH}"
artifact_revision = "{vision_revision}"
backend = "transformers_local"
dtype = "bfloat16"
tensor_parallel_size = 1
max_model_len = 12288
instance_cpu_num = 4
instance_host_mem_mb = 16384
weight_hbm_mb = 8192
runtime_hbm_mb = 3072
kv_cache_hbm_mb = 512
instance_hbm_mb = 11776
npu_slots = 1
allow_colocation = true
request_capacity = 1
required_capabilities = ["transformers_local"]
min_replicas = 0
max_replicas = {max_replicas}
target_route_utilization = 1.0
scale_up_pending_threshold = 1
scale_up_sustain_ms = 0
scale_down_idle_ms = {scale_down_idle_ms}
scale_cooldown_ms = {scale_cooldown_ms}
max_parallel_starts = {max_parallel_starts}
startup_timeout_ms = 600000
drain_timeout_ms = 120000

[models.launch_options]
enable_thinking = false
generation_method = "manual_greedy"
model_kind = "vision_language"
qwen2_5_vl_cpu_unique_consecutive_workaround = true
request_timeout_ms = 600000
trust_remote_code = false

[models.warmup_request]
messages = [{{ role = "user", content = "Reply with exactly: ready" }}]
max_tokens = 8
temperature = 0.0
'''


def _node_config(node_id: int) -> str:
    node_ip = f"172.30.240.{10 + node_id}"
    return f"""schema_version = 1
cluster_id = "ascend-maze-logical"
node_id = "node-{node_id}"
node_ip = "{node_ip}"
controller_endpoint = "{CONTROLLER_IP}:{NODE_RPC_PORT}"
authorization_token_file = "/workspace/state/control-plane/cluster.token"
runtime_directory = "/workspace/state/control-plane/node-agent"
worker_rpc_bind_address = "0.0.0.0:0"
worker_advertised_host = "{node_ip}"
ray_temp_directory = "/workspace/state/control-plane/ray-worker"
ray_num_cpus = 20
recording_root_directory = "/workspace/state/control-plane/records"
device_mappings = [{{ physical_device_id = "{node_id}", runtime_visible_device_id = "0", visible_device_index = 0 }}]
"""


def prepare(state_root: Path, *, profile: str = "correctness") -> None:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_path = state_root / "cluster.token"
    if token_path.is_file():
        token = token_path.read_bytes()
        if not token:
            raise RuntimeError(f"cluster token is empty: {token_path}")
    else:
        token = secrets.token_bytes(32)
        _write_private(token_path, token)
    _write_private(
        state_root / "node-0" / "control-plane" / "controller.toml",
        _controller_config(profile),
    )
    _write_private(
        state_root / "node-0" / "control-plane" / "model_catalog.toml",
        _model_catalog(profile),
    )
    for node_id in range(NODE_COUNT):
        node_root = state_root / f"node-{node_id}" / "control-plane"
        _write_private(node_root / "cluster.token", token)
        if node_id > 0:
            _write_private(node_root / "node.toml", _node_config(node_id))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument(
        "--profile",
        choices=("correctness", "performance"),
        default="correctness",
    )
    args = parser.parse_args()
    prepare(args.state_root.expanduser().resolve(), profile=args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
