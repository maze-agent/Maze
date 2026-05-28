# Distributed GPU Smoke

This example validates Maze placement across a Ray cluster with one visible GPU per worker node.

It checks:

- Ray has multiple live nodes.
- Ray workers are registered with Maze core.
- Maze can schedule independent `gpu: 1` tasks across registered nodes.
- Task code sees the expected `CUDA_VISIBLE_DEVICES`.
- The run prints a compact placement table.

## Start The Cluster

On the head machine:

```bash
conda activate maze
ray start --head --port=6379

cd /root/data/Maze
PYTHONPATH=/root/data/Maze maze start --head --port 8000 --ray-head-port 6379
```

On each worker machine:

```bash
conda activate maze
ray start --address='<head-ip>:6379'
```

Joining Ray is not enough for Maze scheduling. Each worker must also register its resources with Maze core. The preferred production path is:

```bash
maze start --worker --addr <head-ip>:8000
```

If workers already joined Ray manually, use the helper from the head machine:

```bash
python examples/distributed_gpu_smoke/register_ray_workers.py \
  --server-url http://<head-ip>:8000
```

## Run The Smoke

From the head machine:

```bash
python examples/distributed_gpu_smoke/validate.py \
  --server-url http://<head-ip>:8000 \
  --register-workers \
  --num-tasks 4
```

Expected output shape:

```text
task_name      maze_task_id  node_id     node_ip        cuda_visible_devices  gpu_name
gpu_probe_0    ...           ...         10.0.0.12      0                     NVIDIA ...
gpu_probe_1    ...           ...         10.0.0.13      0                     NVIDIA ...
gpu_probe_2    ...           ...         10.0.0.14      0                     NVIDIA ...
gpu_probe_3    ...           ...         10.0.0.15      0                     NVIDIA ...
```

The first milestone is healthy when the table shows multiple node IPs and all GPU tasks complete.

