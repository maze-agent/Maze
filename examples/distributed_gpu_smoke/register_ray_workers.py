#!/usr/bin/env python
"""Register Ray worker nodes with Maze core.

This is useful when machines already joined Ray with:

    ray start --address='<head-ip>:6379'

Maze also needs worker resources registered through /start_worker before the
scheduler can place work on those nodes.
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlparse

import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy


def _head_addr_for_workers(server_url: str, head_ip: str) -> str:
    parsed = urlparse(server_url)
    host = parsed.hostname or head_ip
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = head_ip
    return f"{host}:{port}"


def _find_head_node(nodes: list[dict]) -> dict:
    for node in nodes:
        if node.get("Alive") and node.get("Resources", {}).get("node:__internal_head__"):
            return node
    for node in nodes:
        if node.get("Alive"):
            return node
    raise RuntimeError("No alive Ray nodes found")


@ray.remote
def _register_current_node(head_addr: str) -> dict:
    import ray
    import requests

    from maze.utils.utils import collect_gpu_info

    current_node_id = ray.get_runtime_context().get_node_id()
    current_node = None
    for node in ray.nodes():
        if node["NodeID"] == current_node_id and node.get("Alive"):
            current_node = node
            break

    if current_node is None:
        raise RuntimeError(f"Cannot find current Ray node {current_node_id}")

    resources = {
        "cpu": current_node["Resources"].get("CPU", 0),
        "cpu_mem": current_node["Resources"].get("memory", 0),
        "gpu_resource": {},
    }
    for gpu in collect_gpu_info():
        gpu_id = gpu["index"]
        resources["gpu_resource"][gpu_id] = {
            "gpu_id": gpu_id,
            "gpu_mem": gpu["memory_free"],
            "gpu_num": 1,
        }

    payload = {
        "node_ip": current_node["NodeManagerAddress"],
        "node_id": current_node_id,
        "resources": resources,
    }
    response = requests.post(f"http://{head_addr}/start_worker", json=payload, timeout=10)
    response.raise_for_status()

    return {
        "node_id": current_node_id,
        "node_ip": current_node["NodeManagerAddress"],
        "resources": resources,
        "response": response.json(),
    }


def register_ray_workers(server_url: str, include_head: bool = False) -> list[dict]:
    ray.init(address="auto")
    nodes = [node for node in ray.nodes() if node.get("Alive")]
    head_node = _find_head_node(nodes)
    head_ip = head_node["NodeManagerAddress"]
    head_addr = _head_addr_for_workers(server_url, head_ip)

    refs = []
    for node in nodes:
        is_head = node["NodeID"] == head_node["NodeID"]
        if is_head and not include_head:
            continue
        strategy = NodeAffinitySchedulingStrategy(node_id=node["NodeID"], soft=False)
        refs.append(_register_current_node.options(scheduling_strategy=strategy).remote(head_addr))

    return ray.get(refs) if refs else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Register live Ray nodes with Maze core")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000", help="Maze core URL")
    parser.add_argument("--include-head", action="store_true", help="Also register the Ray head node")
    args = parser.parse_args()

    results = register_ray_workers(args.server_url, include_head=args.include_head)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

