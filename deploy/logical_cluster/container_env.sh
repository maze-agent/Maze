#!/usr/bin/env bash

# Shared runtime environment for every logical Ascend-Maze compute node.
set -o allexport

export ASCEND_HOME_PATH="/usr/local/Ascend/cann-9.0.0-beta.2"
export ASCEND_TOOLKIT_HOME="${ASCEND_HOME_PATH}"
export ASCEND_OPP_PATH="${ASCEND_HOME_PATH}/opp"
export ASCEND_AICPU_PATH="${ASCEND_HOME_PATH}"
export TOOLCHAIN_HOME="${ASCEND_HOME_PATH}/toolkit"

export ASCEND_VISIBLE_DEVICES="${ASCEND_VISIBLE_DEVICES:-0}"
export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-0}"
export ASCEND_DEVICE_ID="${ASCEND_DEVICE_ID:-0}"

if [[ -z "${ASCEND_MAZE_REPO_ROOT:-}" ]]; then
    ASCEND_MAZE_ENV_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    ASCEND_MAZE_REPO_ROOT="$(cd -- "${ASCEND_MAZE_ENV_DIR}/../.." && pwd)"
fi
export ASCEND_MAZE_REPO_ROOT
export ASCEND_MAZE_CONDA_ROOT="${ASCEND_MAZE_CONDA_ROOT:-/home/user2/workplace/miniconda3}"
export CONDA_PREFIX="${ASCEND_MAZE_CONDA_ROOT}/envs/ascend-maze"
export PATH="${CONDA_PREFIX}/bin:${ASCEND_HOME_PATH}/bin:${ASCEND_HOME_PATH}/tools/ccec_compiler/bin:${PATH}"
export PYTHONPATH="${ASCEND_MAZE_REPO_ROOT}/src:${ASCEND_MAZE_REPO_ROOT}:${ASCEND_HOME_PATH}/python/site-packages:${ASCEND_HOME_PATH}/opp/built-in/op_impl/ai_core/tbe${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${ASCEND_HOME_PATH}/lib64:${ASCEND_HOME_PATH}/lib64/plugin/opskernel:${ASCEND_HOME_PATH}/lib64/plugin/nnengine:${ASCEND_HOME_PATH}/opp/built-in/op_impl/ai_core/tbe/op_tiling/lib/linux/aarch64:/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

export HOME="${HOME:-/workspace/state/home}"
export TMPDIR="${TMPDIR:-/workspace/state/tmp}"
export RAY_TMPDIR="${RAY_TMPDIR:-/workspace/state/ray}"
export ASCEND_PROCESS_LOG_PATH="${ASCEND_PROCESS_LOG_PATH:-/ascend/log}"
export PYTHONDONTWRITEBYTECODE=1
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

set +o allexport
