#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly NODE_COUNT=8
readonly CLUSTER_NAME="ascend-maze-logical"
readonly NETWORK_NAME="${CLUSTER_NAME}-network"
readonly NETWORK_SUBNET="172.30.240.0/24"
readonly IMAGE="${ASCEND_MAZE_CONTAINER_IMAGE:-quay.io/openeuler/openeuler@sha256:f4f7b49430a7eec2f8dfd9d25d6d677cb81aa7ccbf2cd4cac70c4f9acc39f7ee}"
readonly STATE_ROOT="${ASCEND_MAZE_CONTAINER_STATE_ROOT:-${HOME}/.local/state/ascend-maze/logical-cluster}"
readonly CONDA_ROOT="${ASCEND_MAZE_CONDA_ROOT:-/home/user2/workplace/miniconda3}"
readonly MODEL_ROOT="${ASCEND_MAZE_MODEL_ROOT:-/home/user2/workplace/model_weight}"
readonly ENV_SCRIPT="${REPO_ROOT}/deploy/logical_cluster/container_env.sh"
readonly VERIFY_SCRIPT="${REPO_ROOT}/deploy/logical_cluster/verify_node.py"
readonly VERIFY_BINDING_SCRIPT="${REPO_ROOT}/deploy/logical_cluster/verify_device_binding.py"
readonly PREPARE_CONTROL_SCRIPT="${REPO_ROOT}/deploy/logical_cluster/prepare_control_plane.py"
readonly VERIFY_CONTROL_SCRIPT="${REPO_ROOT}/deploy/logical_cluster/verify_control_plane.py"
readonly TINI_VERSION="0.19.0"
readonly TINI_SHA256="eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9"
readonly TINI_PATH="${STATE_ROOT}/bin/tini-static-arm64"
readonly USER_ID="$(id -u)"
readonly GROUP_ID="$(id -g)"

# Each NPU receives ten local and ten adjacent-NUMA CPUs. The remaining 32
# CPUs stay outside the containers for the controller, client and host monitor.
readonly -a CPUSETS=(
    "144-153,168-177"
    "156-165,180-189"
    "96-105,120-129"
    "108-117,132-141"
    "0-9,24-33"
    "12-21,36-45"
    "48-57,72-81"
    "60-69,84-93"
)
readonly -a MEMSETS=("6,7" "6,7" "4,5" "4,5" "0,1" "0,1" "2,3" "2,3")

usage() {
    cat <<'EOF'
Usage: logical_cluster.sh COMMAND [ARGS]

Commands:
  up                 Create and start all eight logical compute nodes.
  down               Remove the logical compute nodes and their Docker network.
  status             Show container and resource-assignment status.
  verify [NODE_ID]   Run CPU, memory and NPU checks on all nodes or one node.
  verify-binding N   Verify runtime-to-physical NPU binding and HBM cleanup.
  control-up [PROFILE]
                     Start the control plane with correctness (default) or performance.
  control-status     Show Controller, NodeAgent and Ray cluster status.
  control-down       Stop NodeAgents and the Controller control plane.
  shell NODE_ID      Open an interactive shell in one node.
  exec NODE_ID CMD   Run a command in one node with the CANN environment loaded.
EOF
}

container_name() {
    printf '%s-node-%s' "${CLUSTER_NAME}" "$1"
}

validate_node_id() {
    local node_id="$1"
    if [[ ! "${node_id}" =~ ^[0-7]$ ]]; then
        echo "node ID must be within 0..7: ${node_id}" >&2
        exit 2
    fi
}

ensure_prerequisites() {
    local command
    for command in docker curl sha256sum; do
        if ! command -v "${command}" >/dev/null; then
            echo "required host command is missing: ${command}" >&2
            exit 1
        fi
    done
    for path in "${REPO_ROOT}" "${CONDA_ROOT}/envs/ascend-maze" "${MODEL_ROOT}" \
        /usr/local/Ascend /usr/local/bin/npu-smi \
        /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc \
        /dev/dvpp_cmdlist; do
        if [[ ! -e "${path}" ]]; then
            echo "required host path is missing: ${path}" >&2
            exit 1
        fi
    done
    for node_id in $(seq 0 7); do
        if [[ ! -e "/dev/davinci${node_id}" ]]; then
            echo "required NPU device is missing: /dev/davinci${node_id}" >&2
            exit 1
        fi
    done
    if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
        docker pull "${IMAGE}"
    fi
    local architecture
    architecture="$(docker image inspect "${IMAGE}" --format '{{.Architecture}}')"
    if [[ "${architecture}" != "arm64" ]]; then
        echo "container image must be arm64, found ${architecture}: ${IMAGE}" >&2
        exit 1
    fi
}

ensure_network() {
    if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
        docker network create \
            --driver bridge \
            --subnet "${NETWORK_SUBNET}" \
            --label com.ascend-maze.logical-cluster=true \
            "${NETWORK_NAME}" >/dev/null
    fi
}

ensure_tini() {
    local expected="${TINI_SHA256}  ${TINI_PATH}"
    if [[ -x "${TINI_PATH}" ]] && printf '%s\n' "${expected}" | sha256sum --check --status; then
        return
    fi

    local temporary="${TINI_PATH}.tmp.$$"
    mkdir -p "$(dirname "${TINI_PATH}")"
    rm -f "${temporary}"
    if ! curl --fail --location --retry 3 --silent --show-error \
        "https://github.com/krallin/tini/releases/download/v${TINI_VERSION}/tini-static-arm64" \
        --output "${temporary}"; then
        rm -f "${temporary}"
        return 1
    fi
    if ! printf '%s  %s\n' "${TINI_SHA256}" "${temporary}" | \
        sha256sum --check --status; then
        echo "downloaded Tini checksum does not match the pinned digest" >&2
        rm -f "${temporary}"
        return 1
    fi
    chmod 0755 "${temporary}"
    mv "${temporary}" "${TINI_PATH}"
}

prepare_state() {
    local node_id="$1"
    local node_state="${STATE_ROOT}/node-${node_id}"
    mkdir -p \
        "${node_state}/home" \
        "${node_state}/logs" \
        "${node_state}/output" \
        "${node_state}/ray" \
        "${node_state}/tmp"
}

create_node() {
    local node_id="$1"
    local name
    local node_state="${STATE_ROOT}/node-${node_id}"
    local ip_suffix="$((10 + node_id))"
    name="$(container_name "${node_id}")"

    if docker inspect "${name}" >/dev/null 2>&1; then
        if [[ "$(docker inspect "${name}" --format '{{.State.Running}}')" == "true" ]]; then
            echo "${name}: already running"
            return
        fi
        docker rm "${name}" >/dev/null
    fi

    docker run --detach \
        --name "${name}" \
        --hostname "node-${node_id}" \
        --label com.ascend-maze.logical-cluster=true \
        --label "com.ascend-maze.logical-node=${node_id}" \
        --network "${NETWORK_NAME}" \
        --ip "172.30.240.${ip_suffix}" \
        --user "${USER_ID}:${GROUP_ID}" \
        --workdir /workspace/state \
        --cpuset-cpus "${CPUSETS[${node_id}]}" \
        --cpuset-mems "${MEMSETS[${node_id}]}" \
        --memory 240g \
        --memory-swap 240g \
        --shm-size 16g \
        --pids-limit 8192 \
        --ulimit nofile=1048576:1048576 \
        --device "/dev/davinci${node_id}:/dev/davinci0" \
        --device /dev/davinci_manager \
        --device /dev/devmm_svm \
        --device /dev/hisi_hdc \
        --device /dev/dvpp_cmdlist \
        --volume /usr/local/Ascend:/usr/local/Ascend:ro \
        --volume /usr/local/bin/npu-smi:/usr/local/bin/npu-smi:ro \
        --volume /etc/ascend_install.info:/etc/ascend_install.info:ro \
        --volume /etc/hccn.conf:/etc/hccn.conf:ro \
        --volume /etc/passwd:/etc/passwd:ro \
        --volume /etc/group:/etc/group:ro \
        --volume "${CONDA_ROOT}:${CONDA_ROOT}:ro" \
        --volume "${MODEL_ROOT}:${MODEL_ROOT}:ro" \
        --volume "${REPO_ROOT}:${REPO_ROOT}:ro" \
        --volume "${TINI_PATH}:/usr/local/bin/tini:ro" \
        --volume "${node_state}:/workspace/state" \
        --volume "${node_state}/logs:/ascend/log" \
        --env HOME=/workspace/state/home \
        --env TMPDIR=/workspace/state/tmp \
        --env RAY_TMPDIR=/workspace/state/ray \
        --env ASCEND_PROCESS_LOG_PATH=/ascend/log \
        --env "ASCEND_MAZE_REPO_ROOT=${REPO_ROOT}" \
        --env "ASCEND_MAZE_CONDA_ROOT=${CONDA_ROOT}" \
        --env ASCEND_VISIBLE_DEVICES=0 \
        --env ASCEND_RT_VISIBLE_DEVICES=0 \
        --env ASCEND_DEVICE_ID=0 \
        --env "ASCEND_PHYSICAL_DEVICE_ID=${node_id}" \
        --env "ASCEND_MAZE_LOGICAL_NODE_ID=node-${node_id}" \
        --entrypoint /usr/local/bin/tini \
        "${IMAGE}" \
        -- bash -lc "source '${ENV_SCRIPT}'; exec sleep infinity" >/dev/null
    echo "${name}: started (NPU ${node_id}, CPUs ${CPUSETS[${node_id}]}, MEMs ${MEMSETS[${node_id}]})"
}

up() {
    ensure_prerequisites
    ensure_network
    mkdir -p "${STATE_ROOT}"
    ensure_tini
    for node_id in $(seq 0 7); do
        prepare_state "${node_id}"
        create_node "${node_id}"
    done
    status
}

down() {
    local name
    for node_id in $(seq 0 7); do
        name="$(container_name "${node_id}")"
        if docker inspect "${name}" >/dev/null 2>&1; then
            docker rm --force "${name}" >/dev/null
            echo "${name}: removed"
        fi
    done
    if docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
        docker network rm "${NETWORK_NAME}" >/dev/null
    fi
}

status() {
    printf '%-28s %-12s %-8s %-24s %-8s\n' NAME STATUS NPU CPUSET MEMSET
    local name running
    for node_id in $(seq 0 7); do
        name="$(container_name "${node_id}")"
        if docker inspect "${name}" >/dev/null 2>&1; then
            running="$(docker inspect "${name}" --format '{{if .State.Running}}running{{else}}{{.State.Status}}{{end}}')"
        else
            running="absent"
        fi
        printf '%-28s %-12s %-8s %-24s %-8s\n' \
            "${name}" "${running}" "${node_id}" "${CPUSETS[${node_id}]}" "${MEMSETS[${node_id}]}"
    done
}

verify_one() {
    local node_id="$1"
    local name
    name="$(container_name "${node_id}")"
    echo "verifying ${name}"
    docker exec "${name}" bash -lc \
        "source '${ENV_SCRIPT}'; exec python '${VERIFY_SCRIPT}'"
}

verify() {
    ensure_prerequisites
    if [[ $# -eq 1 ]]; then
        validate_node_id "$1"
        verify_one "$1"
        return
    fi
    for node_id in $(seq 0 7); do
        verify_one "${node_id}"
    done
}

verify_binding() {
    local node_id="$1"
    validate_node_id "${node_id}"
    bash -lc \
        "source '${ENV_SCRIPT}'; exec python '${VERIFY_BINDING_SCRIPT}' \
        --node-id 'node-${node_id}' \
        --physical-device-id '${node_id}' \
        --runtime-visible-device-id '0' \
        --visible-device-index 0 \
        --container-name '$(container_name "${node_id}")' \
        --host-state-directory '${STATE_ROOT}/node-${node_id}' \
        --environment-script '${ENV_SCRIPT}' \
        --script-container-path '${VERIFY_BINDING_SCRIPT}'"
}

container_is_running() {
    local node_id="$1"
    [[ "$(docker inspect "$(container_name "${node_id}")" --format '{{.State.Running}}' 2>/dev/null || true)" == "true" ]]
}

control_prepare() {
    local profile="$1"
    python "${PREPARE_CONTROL_SCRIPT}" \
        --state-root "${STATE_ROOT}" \
        --profile "${profile}"
}

wait_for_controller() {
    local attempts=90
    while (( attempts > 0 )); do
        if docker exec "$(container_name 0)" bash -lc \
            "source '${ENV_SCRIPT}'; python -m ascend_maze.cli.main --json \
            controller status --socket /workspace/state/control-plane/control.sock" \
            >/dev/null 2>&1; then
            return
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    echo "Controller did not become ready; see ${STATE_ROOT}/node-0/control-plane/controller.log" >&2
    exit 1
}

control_up() {
    local profile="${1:-correctness}"
    if [[ "${profile}" != "correctness" && "${profile}" != "performance" ]]; then
        echo "control profile must be correctness or performance: ${profile}" >&2
        exit 2
    fi
    for node_id in $(seq 0 7); do
        if ! container_is_running "${node_id}"; then
            echo "logical containers are not all running; run: $0 up" >&2
            exit 1
        fi
    done
    control_prepare "${profile}"
    if ! controller_is_running; then
        rm -f "${STATE_ROOT}/node-0/control-plane/control.sock"
        docker exec --detach "$(container_name 0)" bash -lc \
            "source '${ENV_SCRIPT}'; exec python -m ascend_maze.cli.main \
            controller start \
            --config /workspace/state/control-plane/controller.toml \
            >> /workspace/state/control-plane/controller.log 2>&1"
    fi
    wait_for_controller
    for node_id in $(seq 1 7); do
        if node_agent_is_running "${node_id}"; then
            continue
        fi
        docker exec --detach "$(container_name "${node_id}")" bash -lc \
            "source '${ENV_SCRIPT}'; exec python -m ascend_maze.cli.main node start \
            --config /workspace/state/control-plane/node.toml \
            >> /workspace/state/control-plane/node-agent.log 2>&1"
    done
    local attempts=120
    while (( attempts > 0 )); do
        local healthy
        healthy="$(docker exec "$(container_name 0)" bash -lc \
            "source '${ENV_SCRIPT}'; python -m ascend_maze.cli.main --json \
            cluster nodes --socket /workspace/state/control-plane/control.sock" \
            2>/dev/null | python -c \
            'import json,sys; p=json.load(sys.stdin); nodes=p["cluster"]["nodes"]; print(sum(n["status"] == "healthy" for n in nodes))' \
            2>/dev/null || true)"
        if [[ "${healthy}" == "8" ]]; then
            control_status
            return
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    echo "eight healthy NodeAgents did not register before deadline" >&2
    control_status || true
    exit 1
}

node_agent_is_running() {
    local node_id="$1"
    docker exec "$(container_name "${node_id}")" \
        "${CONDA_ROOT}/envs/ascend-maze/bin/python" -c \
        'import json,os,pathlib,sys
path=pathlib.Path(sys.argv[1])
try:
    payload=json.loads(path.read_text(encoding="utf-8"))
    pid=int(payload["pid"])
    fields=pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    if int(fields[21]) != int(payload["process_start_ticks"]):
        sys.exit(1)
    os.kill(pid, 0)
except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, KeyError, IndexError):
    sys.exit(1)' "/workspace/state/control-plane/node-agent/node.pid" \
        >/dev/null 2>&1
}

controller_is_running() {
    docker exec "$(container_name 0)" \
        "${CONDA_ROOT}/envs/ascend-maze/bin/python" -c \
        'import json,os,pathlib,sys
path=pathlib.Path(sys.argv[1])
try:
    payload=json.loads(path.read_text(encoding="utf-8"))
    pid=int(payload["pid"])
    fields=pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    if int(fields[21]) != int(payload["process_start_ticks"]):
        sys.exit(1)
    os.kill(pid, 0)
except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, KeyError, IndexError):
    sys.exit(1)' "/workspace/state/control-plane/controller.pid" \
        >/dev/null 2>&1
}

control_status() {
    echo "Controller:"
    docker exec "$(container_name 0)" bash -lc \
        "source '${ENV_SCRIPT}'; python -m ascend_maze.cli.main --json \
        controller status --socket /workspace/state/control-plane/control.sock"
    echo "Nodes and resources:"
    docker exec "$(container_name 0)" bash -lc \
        "source '${ENV_SCRIPT}'; python -m ascend_maze.cli.main --json \
        cluster nodes --socket /workspace/state/control-plane/control.sock"
    echo "Ray nodes:"
    docker exec "$(container_name 0)" bash -lc \
        "source '${ENV_SCRIPT}'; python '${VERIFY_CONTROL_SCRIPT}' \
        --expected-node-count 8"
}

stop_node_agent() {
    local node_id="$1"
    docker exec "$(container_name "${node_id}")" \
        "${CONDA_ROOT}/envs/ascend-maze/bin/python" -c \
        'import json,os,pathlib,signal,sys
path=pathlib.Path(sys.argv[1])
try:
    payload=json.loads(path.read_text(encoding="utf-8"))
    pid=int(payload["pid"])
    fields=pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    if int(fields[21]) == int(payload["process_start_ticks"]):
        os.kill(pid, signal.SIGTERM)
except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, KeyError, IndexError):
    pass' "/workspace/state/control-plane/node-agent/node.pid" \
        >/dev/null 2>&1 || true
}

wait_for_node_agents_to_stop() {
    local attempts=90
    while (( attempts > 0 )); do
        local running=0
        for node_id in $(seq 1 7); do
            if node_agent_is_running "${node_id}"; then
                running=1
                break
            fi
        done
        if (( running == 0 )); then
            return
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    echo "NodeAgents did not release their PID locks before deadline" >&2
    return 1
}

wait_for_controller_to_stop() {
    # A performance profile may own many Standby Workers. Their bounded,
    # sequential retirement can legitimately outlive the control RPC deadline.
    local attempts=360
    while (( attempts > 0 )); do
        if ! controller_is_running; then
            return
        fi
        sleep 1
        attempts=$((attempts - 1))
    done
    echo "Controller did not release its PID lock before cleanup deadline" >&2
    return 1
}

control_down() {
    # Keep NodeAgents available while the Controller releases remote model,
    # Worker and Placement leases. The CLI may hit its short RPC deadline while
    # shutdown continues server-side, so the process lock is authoritative.
    if controller_is_running && \
        [[ -S "${STATE_ROOT}/node-0/control-plane/control.sock" ]]; then
        docker exec "$(container_name 0)" bash -lc \
            "source '${ENV_SCRIPT}'; python -m ascend_maze.cli.main controller stop \
            --config /workspace/state/control-plane/controller.toml --force" || true
        wait_for_controller_to_stop
    fi
    for node_id in $(seq 1 7); do
        stop_node_agent "${node_id}"
    done
    wait_for_node_agents_to_stop
}

node_shell() {
    local node_id="$1"
    validate_node_id "${node_id}"
    docker exec --interactive --tty "$(container_name "${node_id}")" bash -lc \
        "source '${ENV_SCRIPT}'; cd '${REPO_ROOT}'; exec bash"
}

node_exec() {
    local node_id="$1"
    shift
    validate_node_id "${node_id}"
    if [[ $# -eq 0 ]]; then
        echo "exec requires a command" >&2
        exit 2
    fi
    local quoted=""
    printf -v quoted '%q ' "$@"
    docker exec "$(container_name "${node_id}")" bash -lc \
        "source '${ENV_SCRIPT}'; cd '${REPO_ROOT}'; exec ${quoted}"
}

command="${1:-}"
case "${command}" in
    up)
        up
        ;;
    down)
        down
        ;;
    status)
        status
        ;;
    verify)
        shift
        verify "$@"
        ;;
    verify-binding)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        verify_binding "$2"
        ;;
    control-up)
        [[ $# -le 2 ]] || { usage >&2; exit 2; }
        control_up "${2:-correctness}"
        ;;
    control-status)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        control_status
        ;;
    control-down)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        control_down
        ;;
    shell)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        node_shell "$2"
        ;;
    exec)
        [[ $# -ge 3 ]] || { usage >&2; exit 2; }
        shift
        node_exec "$@"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
