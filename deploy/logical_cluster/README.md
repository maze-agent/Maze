# Eight-node logical Ascend cluster

This deployment divides the physical 8-NPU host into eight Docker compute
nodes. Each node receives one logical NPU, 20 exclusive CPU cores, a 240 GiB
memory limit and a 16 GiB `/dev/shm`.

Each container runs a pinned upstream Tini binary as PID 1. It reaps orphaned
Ray and Worker child processes after control-plane shutdown so repeated trials
do not accumulate zombie processes inside the logical nodes. The deployment
downloads and verifies Tini 0.19.0 on the first `up`, then keeps it under the
state root rather than the repository.

The deployment does not copy CANN, Conda environments, source code or model
weights. It mounts the host installations read-only and stores mutable state
under:

```text
~/.local/state/ascend-maze/logical-cluster/
```

## Commands

```bash
deploy/logical_cluster/logical_cluster.sh up
deploy/logical_cluster/logical_cluster.sh verify
deploy/logical_cluster/logical_cluster.sh verify-binding 3
deploy/logical_cluster/logical_cluster.sh status
deploy/logical_cluster/logical_cluster.sh control-up
deploy/logical_cluster/logical_cluster.sh control-status
deploy/logical_cluster/logical_cluster.sh shell 0
deploy/logical_cluster/logical_cluster.sh exec 0 python -V
deploy/logical_cluster/logical_cluster.sh control-down
deploy/logical_cluster/logical_cluster.sh down
```

`verify` checks CPU affinity, the cgroup memory limit, single-device visibility
and a real NPU Tensor operation on every node. It also verifies the Ray and
Ascend-Maze imports and both shared model directories.

`verify-binding N` constructs a real `DeviceBinding` in a child Worker. It
checks that CANN uses runtime device `0`, DCMI reports the Worker only on host
physical NPU `N`, and HBM returns to the pre-Worker baseline after exit.

`control-up` uses node-0 as the Ray Head, Controller and first NodeAgent. It
joins node-1 through node-7 as Ray workers and NodeAgents, then waits until both
the Controller and Ray report eight healthy logical nodes. Private generated
configuration and the cluster token remain under the state root; they are not
written to the repository.

Use the explicit performance profile for Maze/Ray comparisons:

```bash
deploy/logical_cluster/logical_cluster.sh control-up performance
```

This profile enables HACS without tensor parallelism, static resource anchors,
two task slots per NPU, colocation, one idle CPU/I/O/NPU-host Standby Worker per
logical node and up to eight model replicas. `max_tasks_per_worker` remains one
because NPU Attempts are process isolated. The generated config and model
catalog are frozen into each performance plan.

## Paired performance pilot

Run the first paired workload from the host after the performance control plane
has reached eight healthy nodes:

```bash
/home/user2/workplace/miniconda3/envs/ascend-maze/bin/python \
  tools/logical_cluster_performance.py \
  --executor paired \
  --mode batch --mode arrival \
  --batch-size 1 --batch-size 2 \
  --arrival-ratio 0.25 \
  --output-dir \
  ~/.local/state/ascend-maze/logical-cluster/node-0/output/qwen3-retail-paired-pilot
```

The pilot uses `Qwen3-4B`, Transformers `manual_greedy` and
`tbench.retail_cancel`. Both executors use `max_tokens=4096`, temperature zero
and `max_model_len=10240`. Ray uses `max_calls=1` and reserves all 20 CPUs of a
logical node for each Workflow Task so that each logical Ray node admits at
most one Task at a time.

Batch size is the number of requests admitted together. Arrival ratio is
`arrival_rate * average_workflow_seconds`; the default 0.25 case uses a
30-second reference duration and a 130-second admission window. Request E2E
starts before Maze submission preparation or Ray dispatch and ends after the
terminal result returns. Maze `DestroyRun` cleanup is recorded separately and
is not included in E2E.

Each case first samples a three-second idle resource baseline. `report.md`
summarizes success, E2E P95, throughput, CPU, NPU utilization and incremental
HBM. `plan.json` freezes execution order, config/catalog hashes and Git state;
`summary.json`, per-case runner records, resource JSONL and stdout/stderr retain
the auditable raw evidence.

The default cases are a correctness and measurement-pipeline pilot. P95 from
one or two requests is not a statistically stable percentile and must not be
reported as a final performance conclusion.

## End-to-end acceptance

Run the cold text and vision acceptance sequence after `control-up`:

```bash
deploy/logical_cluster/logical_cluster.sh exec 0 \
  python tools/logical_cluster_e2e.py \
  --family all \
  --output-dir /workspace/state/output/logical-cluster-e2e-all
```

The cold text Run may place every Task on the same node because its model
reservation does not exist when the first Task is placed. After the first
command has established the logical model instances, require direct
cross-node evidence with:

```bash
deploy/logical_cluster/logical_cluster.sh exec 0 \
  python tools/logical_cluster_e2e.py \
  --family text \
  --require-cross-node-text \
  --output-dir /workspace/state/output/logical-cluster-e2e-text-crossnode
```

Each Run materializes its exit result, destroys its Run data index and waits
for Run-owned leases, active Worker leases and used-device HBM to recover.
`text.json`, `vision.json` and `summary.json` retain the task nodes, physical
NPU evidence, timing, destroy tombstone and recovery snapshots.

The default openEuler image is pinned to the tested ARM64 multi-architecture
manifest digest. Set `ASCEND_MAZE_CONTAINER_IMAGE` only when intentionally
testing another compatible image.

`control-down` keeps NodeAgents alive until the Controller has released remote
model, Worker and Placement leases. A performance control plane can take longer
than the control RPC deadline to retire all Standby Workers; the command waits
for the Controller process lock before stopping the remaining NodeAgents.

## Topology

| Node | Physical NPU | CPUs | NUMA memory nodes | Address |
|---|---:|---|---|---|
| node-0 | 0 | 144-153,168-177 | 6,7 | 172.30.240.10 |
| node-1 | 1 | 156-165,180-189 | 6,7 | 172.30.240.11 |
| node-2 | 2 | 96-105,120-129 | 4,5 | 172.30.240.12 |
| node-3 | 3 | 108-117,132-141 | 4,5 | 172.30.240.13 |
| node-4 | 4 | 0-9,24-33 | 0,1 | 172.30.240.14 |
| node-5 | 5 | 12-21,36-45 | 0,1 | 172.30.240.15 |
| node-6 | 6 | 48-57,72-81 | 2,3 | 172.30.240.16 |
| node-7 | 7 | 60-69,84-93 | 2,3 | 172.30.240.17 |

The host retains 32 CPUs for the Controller, benchmark client and independent
resource monitor. Two containers share each pair of NUMA memory nodes because
the physical topology attaches two NPUs to one local CPU NUMA node.

This is one physical host with eight logical nodes. It is suitable for resource
placement and colocation experiments, but it does not reproduce physical
multi-node network bandwidth, latency or failure isolation.

## Runtime integration boundary

Docker maps host `/dev/davinciN` to `/dev/davinci0` in node N. CANN execution
therefore uses logical device 0, while DCMI and `npu-smi` retain the host
physical device identity N. Each generated NodeAgent configuration explicitly
declares:

```text
physical_device_id=N
runtime_visible_device_id=0
visible_device_index=0
```

The NodeAgent sends this topology in its registration. The Controller stores
it in `RuntimeNodeBinding`, and `DeviceBinding` uses it to configure CANN while
retaining the physical ID for DCMI verification. On bare metal, an omitted
mapping defaults to the compatible identity mapping `N -> N -> 0`.
