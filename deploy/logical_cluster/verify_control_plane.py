#!/usr/bin/env python3
"""Verify the live Ray topology used by the logical Ascend cluster."""

from __future__ import annotations

import argparse
import json

import ray


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-node-count", type=int, default=8)
    args = parser.parse_args()
    ray.init(address="auto")
    try:
        nodes = tuple(
            sorted(
                (
                    {
                        "alive": bool(item["Alive"]),
                        "cpu": int(item["Resources"].get("CPU", 0)),
                        "ip": str(item["NodeManagerAddress"]),
                        "node_id": str(item["NodeID"]),
                    }
                    for item in ray.nodes()
                    if bool(item["Alive"])
                ),
                key=lambda item: item["ip"],
            )
        )
    finally:
        ray.shutdown()
    expected_ips = tuple(
        f"172.30.240.{10 + index}" for index in range(args.expected_node_count)
    )
    observed_ips = tuple(item["ip"] for item in nodes)
    if observed_ips != expected_ips:
        raise RuntimeError(
            f"Ray topology mismatch: expected={expected_ips}, observed={observed_ips}"
        )
    if any(item["cpu"] != 20 for item in nodes):
        raise RuntimeError(f"Ray logical node CPU capacity mismatch: {nodes}")
    print(json.dumps({"nodes": nodes, "status": "ok"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
