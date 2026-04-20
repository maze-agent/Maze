# maze-sc

**maze-sc** is the research artifact tree for **Maze**: a distributed, task-oriented framework for running LLM-heavy agent workloads at scale. The Python package name in this repository remains `agentos` for historical compatibility; paths and APIs below refer to this layout.

## What this repository contributes

Maze (as realized in **maze-sc**) targets three gaps between agentic logic and efficient cluster execution:

1. **Task-level abstraction with resource anchoring**  
   Agent workflows are expressed as executable **DAGs of typed tasks** (`dag.json` topology + `task.py` bodies). Node attributes carry **explicit resource semantics** (CPU, memory, GPU memory, I/O), merged with static inference (`resource_infer.py`) and runtime feedback (for example GPU OOM handling in `task_scheduler.py`). This narrows the gap between coarse “agent” units and schedulable units that clusters can reason about.

2. **HACS: heterogeneity-aware criticality scheduling**  
   The default scheduler backend is **HACS** (`src/agentos/scheduler/hacs.py`), aligned with the paper’s value-density view: extract \((\Omega, T_{\mathrm{pred}}, \Phi)\)\-style features per task, combine with **separate GPU / CPU / I/O dispatch paths** in `TaskScheduler` to reduce cross-resource head-of-line blocking, and use **online runtime prediction** (`exec_time_pred.py`, XGBoost-based) to inform ordering.

3. **Agent-oriented runtime integration**  
   The resource layer couples **Ray**, **Redis**, **Flask** control APIs, **DAG-aware context** (`dag_context.py`), **session sticky routing** toward vLLM replicas for KV reuse, **zero-VRAM standby** workers (`standby_worker.py`), and **elastic inference** hooks (load thresholds, monitoring) in `task_scheduler.py`—aimed at a stable control plane under many fine-grained tasks.

Public-facing SDK surfaces described as **Table II** in the paper may live in the separate **Maze** product repository; this tree focuses on the **JSON DAG + HTTP** experiment path. A concise paper-to-code map is maintained with the paper / artifact materials (not part of this public tree).

## Repository layout (high level)

| Path | Role |
| --- | --- |
| `src/agentos/scheduler/` | Scheduler process (`scheduler.py`) and strategies (`hacs.py`, `fcfs.py`, `atlas.py`, …). |
| `src/agentos/resource/` | Resource / worker side: `api_server.py`, `task_scheduler.py`, `standby_worker.py`, … |
| `src/agentos/workflows/` | Benchmark DAGs: **GAIA**, **τ-bench** (`tbench`), **OpenAGI** (`dag.json` + `task.py` per workflow). |
| `src/agentos/utils/` | Loaders, execution backends, time prediction, helpers. |
| `src/experiment/` | Experiment driver scripts. |

## Prerequisites

- Python **3.10** (recommended; match `requirements.txt` in your environment).
- **Ray** cluster, **Redis**, and GPU nodes as required by your workflows.
- Models and data paths configured for your cluster (defaults in code point at example paths).

## Install

```bash
conda create -n maze-sc python=3.10 -y
conda activate maze-sc
pip install -r requirements.txt
```

## Run the core stack

### 1. Ray and Redis

```bash
# Head node
ray start --head

# Workers (replace with your head address)
ray start --address='<head_ip>:<head_port>'

# Redis (example: port 6380)
redis-server --port 6380 --bind 0.0.0.0 --protected-mode no &
```

### 2. Resource layer (Flask API + workers)

From the repository root (or any cwd on `PYTHONPATH`):

```bash
python src/agentos/resource/api_server.py \
  --redis_ip 127.0.0.1 --redis_port 6380 --flask_port 5000
```

`--proj_path` defaults to the **maze-sc root** inferred from `__file__` (directory that contains `src/agentos`). Override if your checkout layout is nonstandard.

### 3. Scheduler layer (HACS by default)

```bash
python src/agentos/scheduler/scheduler.py \
  --master_addr 127.0.0.1:5000 \
  --redis_ip 127.0.0.1 \
  --redis_port 6380 \
  --strategy hacs \
  --flask_port 5001
```

Shipped strategies in this tree include **`hacs`**, **`fcfs`**, and **`atlas`**. Other strategy names are supported only if matching `agentos.scheduler.*` modules exist on `PYTHONPATH` (lazy import).

### 4. Submit example DAGs

```bash
python src/agentos/agent/dispatch_task.py --master_addr 127.0.0.1:5001
```

The client posts to the scheduler Flask API (`/dag/`, `/status/`, `/get/`, `/release/`). See `src/agentos/agent/agent.py` for a thin Python wrapper.

## Baselines (optional)

Older AgentOS-style **HEFT / CPOP** master/slaver scripts, if present in your deployment, are **not** the default path for maze-sc; prefer the unified `scheduler.py` + `api_server.py` flow above. For artifact evaluation, cite the lazy-import behavior described above and the shipped strategies under `src/agentos/scheduler/`.

## Agent framework integrations

AutoGen / AgentScope integration sections from earlier drafts were removed from this README to avoid stale paths. If you still rely on those drivers, refer to the scripts under your integration package and keep their ports consistent with `scheduler.py` / `dispatch_task.py`.

## Citation

If you use this artifact with the Maze paper, please cite the paper as required by the venue. Supply **paper ↔ code** traceability (paths, equivalence notes) in your artifact description or supplementary materials as required by the venue.

## License

See the license file shipped with the repository (if any). If none is present, retain the upstream project’s terms.
