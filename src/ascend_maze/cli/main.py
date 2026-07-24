"""C13 command line entry point."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import importlib
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from typing import Sequence

import grpc

from ascend_maze import __version__
from ascend_maze.api.workflow import Workflow
from ascend_maze.compiler.compiler import CompileOptions
from ascend_maze.cli.doctor import run_doctor
from ascend_maze.cli.output import emit_error, emit_json, json_value
from ascend_maze.config import (
    LoadedConfig,
    load_config,
    load_config_override_document,
)
from ascend_maze.contracts.data import SharedFileRef
from ascend_maze.control.local_rpc import ControlRpcError, UdsRuntimeClient
from ascend_maze.core.errors import (
    ContractValidationError,
    EnvironmentValidationError,
    ModelValidationError,
    SubmissionConflictError,
)

CONFIG_SCHEMA_VERSION = 1
CONTROL_PROTOCOL_VERSION = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maze", description="Ascend-Maze control CLI")
    parser.add_argument("--version", action="store_true", help="show version identities")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subcommands = parser.add_subparsers(dest="command")

    config = subcommands.add_parser("config", help="validate or render configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    for name in ("validate", "render"):
        command = config_commands.add_parser(name)
        command.add_argument("--config", required=True)

    doctor = subcommands.add_parser("doctor", help="run read-only environment checks")
    doctor.add_argument("--config", required=True)

    controller = subcommands.add_parser("controller")
    controller_commands = controller.add_subparsers(
        dest="controller_command", required=True
    )
    controller_start = controller_commands.add_parser("start")
    controller_start.add_argument("--config", required=True)
    controller_start.add_argument("--config-overrides")
    controller_start.add_argument("--fresh-recovery", action="store_true")
    controller_status = controller_commands.add_parser("status")
    _connection_options(controller_status)
    controller_stop = controller_commands.add_parser("stop")
    _connection_options(controller_stop)
    controller_stop.add_argument("--force", action="store_true")

    node = subcommands.add_parser("node")
    node_commands = node.add_subparsers(dest="node_command", required=True)
    node_start = node_commands.add_parser("start")
    node_start.add_argument("--config", required=True)
    node_status = node_commands.add_parser("status")
    node_status.add_argument("node_id")
    _connection_options(node_status)
    node_drain = node_commands.add_parser("drain")
    node_drain.add_argument("node_id")
    node_drain.add_argument("--boot-id", default="")
    node_drain.add_argument("--force", action="store_true")
    node_drain.add_argument("--timeout-ms", type=int, default=30_000)
    _connection_options(node_drain)
    node_resume = node_commands.add_parser("resume")
    node_resume.add_argument("node_id")
    node_resume.add_argument("--boot-id", default="")
    _connection_options(node_resume)

    cluster = subcommands.add_parser("cluster")
    cluster_commands = cluster.add_subparsers(dest="cluster_command", required=True)
    for name in ("status", "nodes", "resources", "queues", "workers"):
        command = cluster_commands.add_parser(name)
        command.add_argument("--watch", action="store_true")
        _connection_options(command)

    run = subcommands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    submit = run_commands.add_parser("submit")
    submit.add_argument("workflow_factory")
    submit.add_argument("--inputs", required=True)
    submit.add_argument("--submission-id")
    _connection_options(submit)
    list_runs = run_commands.add_parser("list")
    list_runs.add_argument("--status", default="")
    _connection_options(list_runs)
    for name in ("show", "watch", "events", "result", "cancel", "destroy"):
        command = run_commands.add_parser(name)
        command.add_argument("run_id")
        if name == "events":
            command.add_argument("--cursor")
            command.add_argument("--limit", type=int, default=100)
        elif name == "result":
            command.add_argument("--task", required=True)
            command.add_argument("--output")
        elif name == "cancel":
            command.add_argument("--reason", default="user_cancelled")
        elif name == "destroy":
            command.add_argument("--force", action="store_true")
        _connection_options(command)

    models = subcommands.add_parser("models")
    model_commands = models.add_subparsers(dest="models_command", required=True)
    validate_models = model_commands.add_parser("validate")
    validate_models.add_argument("--config", required=True)
    list_models = model_commands.add_parser("list")
    _connection_options(list_models)
    status_models = model_commands.add_parser("status")
    status_models.add_argument("model_id", nargs="?")
    _connection_options(status_models)
    wait_models = model_commands.add_parser("wait-ready")
    wait_models.add_argument("model_id")
    wait_models.add_argument("--replicas", type=int, default=1)
    _connection_options(wait_models)

    recording = subcommands.add_parser("recording")
    recording_commands = recording.add_subparsers(
        dest="recording_command", required=True
    )
    recording_status = recording_commands.add_parser("status")
    _connection_options(recording_status)
    recording_flush = recording_commands.add_parser("flush")
    recording_flush.add_argument("run_id")
    _connection_options(recording_flush)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.version:
            _version(args.json_output)
            return 0
        if args.command == "config":
            loaded = load_config(
                args.config,
                build_revision=_build_revision(),
                created_at_ms=0,
            )
            if args.config_command == "validate":
                result = {
                    "schema_version": 1,
                    "valid": True,
                    "config_fingerprint": loaded.snapshot.config_fingerprint,
                    "source_path": loaded.snapshot.source_path,
                }
                if args.json_output:
                    emit_json(result)
                else:
                    print(f"valid {loaded.snapshot.config_fingerprint}")
                return 0
            _render_config(loaded, args.json_output)
            return 0
        if args.command == "doctor":
            loaded = load_config(args.config, build_revision=_build_revision())
            report = run_doctor(loaded)
            if args.json_output:
                emit_json(report)
            else:
                for check in report.checks:
                    print(f"{check.status:7} {check.name}: {check.message}")
            return 0 if report.passed else 1
        if args.command == "controller" and args.controller_command == "start":
            from ascend_maze.control.application import ControllerApplication

            loaded = _load_controller_start_config(args)
            if args.fresh_recovery:
                _clear_stopped_controller_recovery(loaded)
            return asyncio.run(ControllerApplication(loaded).run())
        if args.command == "node" and args.node_command == "start":
            from ascend_maze.config import load_node_bootstrap
            from ascend_maze.control.node_application import NodeApplication

            return asyncio.run(NodeApplication(load_node_bootstrap(args.config)).run())
        if args.command == "models" and args.models_command == "validate":
            loaded = load_config(args.config, build_revision=_build_revision())
            result = {
                "schema_version": 1,
                "valid": True,
                "model_catalog_revision": loaded.snapshot.model_catalog_revision,
                "config_fingerprint": loaded.snapshot.config_fingerprint,
            }
            _emit_result(result, args.json_output)
            return 0
        if args.command in {"controller", "node", "cluster", "run", "models", "recording"}:
            return asyncio.run(_remote_command(args))
        parser.print_help(sys.stderr)
        return 2
    except EnvironmentValidationError as exc:
        _emit_cli_error(args, "environment_validation_failed", str(exc))
        return 4
    except ModelValidationError as exc:
        _emit_cli_error(args, "model_validation_failed", str(exc))
        return 2 if getattr(args, "command", None) == "config" else 4
    except ContractValidationError as exc:
        _emit_cli_error(args, "local_validation_failed", str(exc))
        return 2
    except SubmissionConflictError as exc:
        _emit_cli_error(args, "submission_conflict", str(exc))
        return 5
    except ControlRpcError as exc:
        _emit_cli_error(args, exc.error_code, str(exc))
        if exc.error_code in {
            "state_rejected",
            "not_found",
            "request_conflict",
        }:
            return 5
        if exc.error_code in {"control_protocol_invalid", "version_incompatible"}:
            return 3
        return 1
    except TimeoutError as exc:
        _emit_cli_error(args, "deadline_exceeded", str(exc))
        return 5
    except (grpc.RpcError, ConnectionError, OSError) as exc:
        _emit_cli_error(args, "controller_unreachable", str(exc))
        return 3


def _version(as_json: bool) -> None:
    payload = {
        "schema_version": 1,
        "project": "Ascend-Maze",
        "project_version": __version__,
        "build_revision": _build_revision(),
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "control_protocol_version": CONTROL_PROTOCOL_VERSION,
        "python_version": platform.python_version(),
    }
    if as_json:
        emit_json(payload)
        return
    print(
        "Ascend-Maze "
        f"{__version__} build={payload['build_revision']} "
        f"config_schema={CONFIG_SCHEMA_VERSION} "
        f"control_protocol={CONTROL_PROTOCOL_VERSION} "
        f"python={payload['python_version']}"
    )


def _render_config(loaded: object, as_json: bool) -> None:
    assert isinstance(loaded, LoadedConfig)
    payload = {
        "schema_version": 1,
        "source_path": loaded.snapshot.source_path,
        "config_fingerprint": loaded.snapshot.config_fingerprint,
        "model_catalog_revision": loaded.snapshot.model_catalog_revision,
        "resolved": json_value(loaded.snapshot.resolved),
    }
    if as_json:
        emit_json(payload)
        return
    print(f"config_fingerprint = {loaded.snapshot.config_fingerprint}")
    emit_json(payload["resolved"])


def _build_revision() -> str:
    return os.environ.get("ASCEND_MAZE_BUILD_REVISION", "uncommitted")


def _load_controller_start_config(args: argparse.Namespace) -> LoadedConfig:
    if not args.config_overrides:
        return load_config(args.config, build_revision=_build_revision())
    override_document = load_config_override_document(args.config_overrides)
    if override_document.build_revision != _build_revision():
        raise ContractValidationError(
            "config override build_revision does not match Controller"
        )
    loaded = load_config(
        args.config,
        build_revision=_build_revision(),
        config_overrides=override_document.overrides,
    )
    if (
        loaded.snapshot.config_fingerprint
        != override_document.expected_config_fingerprint
    ):
        raise ContractValidationError(
            "config override fingerprint does not match resolved Controller config"
        )
    return loaded


def _clear_stopped_controller_recovery(loaded: LoadedConfig) -> None:
    """Reset recovery only when no Controller endpoint or live PID exists."""

    socket_path = Path(loaded.config.control.socket_path)
    pid_path = Path(loaded.config.control.pid_file)
    if socket_path.exists():
        raise ContractValidationError(
            "fresh recovery is forbidden while the Controller socket exists"
        )
    if pid_path.exists():
        try:
            identity = json.loads(pid_path.read_text(encoding="utf-8"))
            if not isinstance(identity, dict):
                raise ValueError("PID lock is not an object")
            pid = identity.get("pid")
            expected_ticks = identity.get("process_start_ticks")
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid < 1
                or isinstance(expected_ticks, bool)
                or not isinstance(expected_ticks, int)
                or expected_ticks < 1
            ):
                raise ValueError("PID lock identity is invalid")
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            current_ticks = int(fields[21])
        except FileNotFoundError:
            current_ticks = None
            expected_ticks = -1
        except (OSError, UnicodeDecodeError, ValueError, IndexError) as exc:
            raise ContractValidationError(
                "cannot prove that the previous Controller PID is stopped"
            ) from exc
        if current_ticks == expected_ticks:
            raise ContractValidationError(
                "fresh recovery is forbidden while the Controller PID is alive"
            )
        pid_path.unlink()
    recovery = Path(loaded.config.control.recovery_path)
    for path in (
        recovery,
        recovery.with_name(recovery.name + "-wal"),
        recovery.with_name(recovery.name + "-shm"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    recovery.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(recovery.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")
    parser.add_argument("--socket")


async def _remote_command(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    await client.get_controller_status()
    await client.verify_compatibility()
    if args.command == "controller":
        if args.controller_command == "status":
            result = await client.query("GetSystemSnapshot")
            _emit_result(result, args.json_output)
            return 0
        result = await client.shutdown_controller(
            force=args.force,
            drain_timeout_ms=_loaded_from_args(args).config.control.shutdown_drain_timeout_ms
            if args.config
            else 5_000,
        )
        _emit_result(result, args.json_output)
        if result.get("timed_out") is True:
            return 5
        exit_code = result.get("exit_code", 0)
        return exit_code if isinstance(exit_code, int) else 1
    if args.command == "cluster":
        operation = (
            "GetWorkerPools"
            if args.cluster_command == "workers"
            else "GetClusterSnapshot"
        )

        async def read_snapshot() -> dict[str, object]:
            return await client.query(operation, filter=args.cluster_command)

        result = await read_snapshot()
        _emit_result(result, args.json_output)
        if not args.watch:
            return 0
        version = _snapshot_version(result)
        while True:
            received = False
            async for batch in client.watch_cluster(
                after_snapshot_version=version,
                limit=100,
            ):
                received = True
                next_version = batch.get("next_snapshot_version")
                if (
                    isinstance(next_version, bool)
                    or not isinstance(next_version, int)
                    or next_version <= version
                ):
                    raise ControlRpcError(
                        "control_protocol_invalid",
                        "cluster watch snapshot version is invalid",
                    )
                refreshed = await read_snapshot()
                refreshed_version = _snapshot_version(refreshed)
                if refreshed_version < next_version:
                    raise ControlRpcError(
                        "control_protocol_invalid",
                        "cluster snapshot is older than its watch event",
                    )
                _emit_result(refreshed, args.json_output)
                version = refreshed_version
                if batch.get("snapshot_required") is True:
                    break
            if not received:
                raise ControlRpcError(
                    "controller_unreachable", "cluster watch ended without an event"
                )
    if args.command == "node":
        if args.node_command == "status":
            result = await client.query("GetClusterSnapshot", filter="nodes")
            cluster_payload = _mapping(result.get("cluster"), "cluster")
            raw_nodes = cluster_payload.get("nodes")
            if not isinstance(raw_nodes, list):
                raise ControlRpcError("control_protocol_invalid", "cluster nodes are invalid")
            nodes = [
                item
                for item in raw_nodes
                if _mapping(_mapping(item, "node").get("capacity"), "capacity").get(
                    "node_id"
                )
                == args.node_id
            ]
            if not nodes:
                raise ControlRpcError("not_found", f"unknown node: {args.node_id}")
            _emit_result(nodes[0], args.json_output)
            return 0
        operation = "DrainNode" if args.node_command == "drain" else "ResumeNode"
        timeout_ms = int(getattr(args, "timeout_ms", 30_000))
        if timeout_ms < 1:
            raise ContractValidationError("node action timeout_ms must be positive")
        boot_id = args.boot_id
        if not boot_id:
            snapshot = await client.query("GetClusterSnapshot", filter="nodes")
            cluster_payload = _mapping(snapshot.get("cluster"), "cluster")
            raw_nodes = cluster_payload.get("nodes")
            if not isinstance(raw_nodes, list):
                raise ControlRpcError(
                    "control_protocol_invalid", "cluster nodes are invalid"
                )
            matching = [
                _mapping(item, "node")
                for item in raw_nodes
                if _mapping(_mapping(item, "node").get("capacity"), "capacity").get(
                    "node_id"
                )
                == args.node_id
            ]
            if not matching:
                raise ControlRpcError("not_found", f"unknown node: {args.node_id}")
            boot_value = _mapping(matching[0].get("capacity"), "capacity").get(
                "boot_id"
            )
            if not isinstance(boot_value, str) or not boot_value:
                raise ControlRpcError(
                    "control_protocol_invalid", "node boot_id is invalid"
                )
            boot_id = boot_value
        result = await client.node_action(
            operation,
            args.node_id,
            boot_id=boot_id,
            force=bool(getattr(args, "force", False)),
            timeout_seconds=timeout_ms / 1_000,
        )
        _emit_result(result, args.json_output)
        if result.get("timed_out") is True:
            return 5
        exit_code = result.get("exit_code", 0)
        return exit_code if isinstance(exit_code, int) else 1
    if args.command == "run":
        if args.run_command == "submit":
            if not args.config:
                raise ContractValidationError("run submit requires --config")
            loaded = _loaded_from_args(args)
            try:
                inputs_path = Path(args.inputs).expanduser().resolve(strict=True)
                raw_inputs = inputs_path.read_bytes()
            except OSError as exc:
                raise ContractValidationError(
                    f"run inputs file is unavailable: {args.inputs}"
                ) from exc
            if len(raw_inputs) > loaded.config.control.max_inline_control_bytes:
                raise ContractValidationError(
                    "run submit inputs JSON exceeds control.max_inline_control_bytes; "
                    "use the Python RuntimeClient data path"
                )
            try:
                inputs = json.loads(raw_inputs)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractValidationError(f"run inputs are invalid JSON: {exc}") from exc
            if not isinstance(inputs, dict) or any(
                not isinstance(name, str) for name in inputs
            ):
                raise ContractValidationError("run inputs must be a JSON object")
            inputs = {
                name: _decode_cli_input(value) for name, value in inputs.items()
            }
            workflow = _load_workflow_factory(args.workflow_factory)
            workflow.compile(
                CompileOptions(
                    max_literal_value_bytes=loaded.config.workflow.max_literal_value_bytes,
                    max_compiled_literal_bytes=loaded.config.workflow.max_compiled_literal_bytes,
                )
            )
            outcome = await client.submit(
                workflow,
                inputs=inputs,
                submission_id=args.submission_id,
            )
            _emit_result(outcome, args.json_output)
            return 0 if outcome.get("state") == "committed" else 1
        if args.run_command == "list":
            result = await client.query("ListRuns", filter=args.status)
            _emit_result(result, args.json_output)
            return 0
        if args.run_command == "show":
            result = await client.query("GetRun", resource_id=args.run_id)
            _emit_result(result, args.json_output)
            return 0
        if args.run_command == "watch":
            last: dict[str, object] | None = None
            async for batch in client.watch_run(args.run_id):
                last = batch
                _emit_result(batch, args.json_output)
            shown = await client.query("GetRun", resource_id=args.run_id)
            status = str(_mapping(shown.get("run"), "run").get("status"))
            if last is None:
                raise ControlRpcError("state_rejected", "watch ended without a control event")
            return 0 if status == "succeeded" else 1
        if args.run_command == "events":
            result = await client.get_run_events(
                args.run_id,
                cursor=args.cursor,
                limit=args.limit,
            )
            _emit_result(result, args.json_output)
            return 0
        if args.run_command == "result":
            result = await client.materialize_task_result(args.run_id, args.task)
            try:
                encoded = json.dumps(
                    json_value(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except TypeError as exc:
                raise ContractValidationError(
                    "Task result is not JSON-compatible; use the Python RuntimeClient"
                ) from exc
            result_config = _loaded_from_args(args) if args.config else None
            limit = (
                result_config.config.control.max_inline_result_bytes
                if result_config is not None
                else 1_048_576
            )
            if len(encoded) > limit and args.output is None:
                raise ControlRpcError(
                    "state_rejected",
                    "Task result exceeds max_inline_result_bytes; provide --output",
                )
            if args.output is not None:
                output = Path(args.output).expanduser().resolve(strict=False)
                try:
                    output.write_bytes(encoded + b"\n")
                except OSError as exc:
                    raise ContractValidationError(
                        f"cannot write Task result output: {output}"
                    ) from exc
                _emit_result({"output": str(output), "size_bytes": len(encoded)}, args.json_output)
            else:
                _emit_result(result, args.json_output)
            return 0
        if args.run_command == "cancel":
            result = await client.run_action(
                "CancelRun", args.run_id, reason=args.reason
            )
            _emit_result(result, args.json_output)
            return 0
        result = await client.run_action(
            "DestroyRun", args.run_id, force=args.force
        )
        _emit_result(result, args.json_output)
        return 0
    if args.command == "models":
        if args.models_command == "list":
            result = await client.query("GetModelCatalog")
            _emit_result(result, args.json_output)
            return 0
        if args.models_command == "status":
            result = await client.query(
                "GetModelInstances", filter=args.model_id or ""
            )
            _emit_result(result, args.json_output)
            return 0
        result = await client.wait_model_ready(
            args.model_id,
            replicas=args.replicas,
        )
        _emit_result(result, args.json_output)
        return 0
    if args.command == "recording":
        if args.recording_command == "status":
            result = await client.query("GetRecorderStatus")
        else:
            result = await client.run_action("FlushRun", args.run_id)
        _emit_result(result, args.json_output)
        return 0 if result.get("recording_complete", True) else 1
    raise AssertionError("unhandled remote command")


def _client_from_args(args: argparse.Namespace) -> UdsRuntimeClient:
    loaded = _loaded_from_args(args) if getattr(args, "config", None) else None
    raw_socket = getattr(args, "socket", None) or os.environ.get(
        "ASCEND_MAZE_CONTROL_SOCKET"
    )
    if raw_socket is None and loaded is not None:
        raw_socket = loaded.config.control.socket_path
    if raw_socket is None:
        raise ContractValidationError(
            "control.socket_path: provide --socket, --config, or ASCEND_MAZE_CONTROL_SOCKET"
        )
    socket_path = Path(raw_socket).expanduser().resolve(strict=False)
    client = UdsRuntimeClient(
        socket_path,
        max_inline_control_bytes=(
            1_048_576
            if loaded is None
            else loaded.config.control.max_inline_control_bytes
        ),
        shared_filesystem_roots=(
            () if loaded is None else loaded.config.data.shared_filesystem_roots
        ),
    )
    if loaded is not None:
        client.config_fingerprint = loaded.snapshot.config_fingerprint
    return client


def _loaded_from_args(args: argparse.Namespace) -> LoadedConfig:
    cached = getattr(args, "_loaded_config", None)
    if isinstance(cached, LoadedConfig):
        return cached
    path = getattr(args, "config", None)
    loaded = load_config(path, build_revision=_build_revision())
    setattr(args, "_loaded_config", loaded)
    return loaded


def _emit_result(value: object, as_json: bool) -> None:
    if as_json:
        normalized = json_value(value)
        if isinstance(normalized, dict):
            normalized = {**normalized, "schema_version": 1}
        else:
            normalized = {"schema_version": 1, "result": normalized}
        emit_json(normalized)
        return
    normalized = json_value(value)
    if isinstance(normalized, dict) and all(
        not isinstance(item, (dict, list)) for item in normalized.values()
    ):
        for key, item in normalized.items():
            print(f"{key}: {item}")
        return
    print(json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlRpcError(
            "control_protocol_invalid", f"{name} must be a JSON object"
        )
    return value


def _snapshot_version(value: object) -> int:
    payload = _mapping(value, "cluster snapshot")
    meta = _mapping(payload.get("meta"), "cluster snapshot meta")
    version = meta.get("snapshot_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ControlRpcError(
            "control_protocol_invalid", "cluster snapshot version is invalid"
        )
    return version


def _decode_cli_input(value: object) -> object:
    if not isinstance(value, dict) or "$shared_file" not in value:
        return value
    if set(value) != {"$shared_file"} or not isinstance(
        value["$shared_file"], dict
    ):
        raise ContractValidationError("invalid explicit SharedFileRef input")
    payload = value["$shared_file"]
    if set(payload) != {"canonical_path", "content_sha256", "size_bytes"}:
        raise ContractValidationError("invalid explicit SharedFileRef fields")
    return SharedFileRef(
        canonical_path=payload["canonical_path"],
        content_sha256=payload["content_sha256"],
        size_bytes=payload["size_bytes"],
    )


def _load_workflow_factory(reference: str) -> Workflow:
    module_reference, separator, attribute = reference.rpartition(":")
    if not separator or not module_reference or not attribute:
        raise ContractValidationError(
            "workflow factory must use MODULE:CALLABLE or FILE.py:CALLABLE"
        )
    try:
        if module_reference.endswith(".py") or Path(module_reference).exists():
            source = Path(module_reference).expanduser().resolve(strict=True)
            module_name = (
                "_ascend_maze_workflow_"
                + hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
            )
            spec = importlib.util.spec_from_file_location(module_name, source)
            if spec is None or spec.loader is None:
                raise ContractValidationError(f"cannot import workflow file: {source}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
        else:
            module = importlib.import_module(module_reference)
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError(
            f"cannot import workflow factory {reference}: {type(exc).__name__}: {exc}"
        ) from exc
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ContractValidationError(f"workflow factory is not callable: {reference}")
    try:
        workflow = factory()
    except Exception as exc:
        raise ContractValidationError(
            f"workflow factory failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(workflow, Workflow):
        raise ContractValidationError("workflow factory must return ascend_maze.Workflow")
    return workflow


def _emit_cli_error(args: argparse.Namespace, error_code: str, message: str) -> None:
    if getattr(args, "json_output", False):
        emit_json(
            {
                "schema_version": 1,
                "status": "error",
                "error_code": error_code,
                "message": message,
            },
            stream=sys.stderr,
        )
        return
    emit_error(f"{error_code}: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
