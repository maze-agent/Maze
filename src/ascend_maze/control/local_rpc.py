"""Typed Head-local ControlService over a protected Unix socket."""

from __future__ import annotations

import asyncio
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import json
import hashlib
import hmac
import os
from pathlib import Path
import stat
from typing import Any, NoReturn, cast

import grpc
import cloudpickle

from ascend_maze import __version__
from ascend_maze.api.workflow import Workflow
from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.data import DataHandle, DataStore, SharedFileRef
from ascend_maze.contracts.submission import (
    SubmissionContract,
    SubmissionOptions,
    SubmissionState,
    hash_session_key,
)
from ascend_maze.control.client import (
    PreparedSubmission,
    normalize_shared_filesystem_roots,
    run_input_identity,
    validate_shared_file_ref,
)
from ascend_maze.control.controller import SubmitRequest
from ascend_maze.control.proto import control_pb2 as _control_pb2
from ascend_maze.control.proto import control_pb2_grpc
from ascend_maze.core.canonical import FrozenMap, canonical_digest, freeze_canonical
from ascend_maze.core.errors import (
    ContractValidationError,
    RunNotTerminalError,
    StateTransitionError,
    SubmissionConflictError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.data.ray_store import RayDataStore, RayDataStoreDescriptor
from ascend_maze.runtime.packaging import build_code_packages

control_pb2: Any = _control_pb2


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    controller_generation: str
    build_revision: str
    environment_fingerprint: str
    healthy_node_count: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.controller_generation,
                self.build_revision,
                self.environment_fingerprint,
            )
        ):
            raise ValueError("controller status identity fields are required")
        if self.healthy_node_count < 0:
            raise ValueError("healthy_node_count must be non-negative")


class _LocalControlServicer:
    def __init__(self, owner: "LocalControlServer") -> None:
        self.owner = owner

    async def GetControllerStatus(
        self,
        request: Any,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> Any:
        if int(request.schema_version) != 1 or not request.request_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "invalid request envelope"
            )
        status = self.owner.status_provider()
        return control_pb2.GetControllerStatusResponse(
            request_id=request.request_id,
            controller_generation=status.controller_generation,
            status_code="ok",
            message="",
            build_revision=status.build_revision,
            environment_fingerprint=status.environment_fingerprint,
            healthy_node_count=status.healthy_node_count,
        )

    async def GetVersion(self, request: Any, context: Any) -> Any:
        del context
        return self._query(
            request.meta,
            {
                "schema_version": 1,
                "project": "Ascend-Maze",
                "project_version": __version__,
                "build_revision": self._api().build_revision,
                "config_schema_version": 1,
                "control_protocol_version": 1,
            },
        )

    async def GetSystemSnapshot(self, request: Any, context: Any) -> Any:
        del context
        api = self._api()
        cluster = api.cluster_snapshot()
        snapshot_version = api.cluster_snapshot_version()
        runs = api.list_runs()
        started_at = getattr(api, "_started_wall_time_ms", None)
        payload = {
            "meta": api.snapshot_meta(snapshot_version=snapshot_version),
            "lifecycle_state": api.lifecycle_state,
            "started_wall_time_ms": started_at,
            "uptime_ms": (
                None if started_at is None else max(0, api.clock.wall_ms() - started_at)
            ),
            "run_count": len(runs),
            "nonterminal_run_count": sum(not item.terminal for item in runs),
            "components": self._component_health_payload(api, cluster),
            "data_owner_generation": api.data_owner_generation,
            "data_store_descriptor": api.data_store_descriptor,
        }
        return self._query(request.meta, payload, snapshot_version=snapshot_version)

    async def GetSubmission(self, request: Any, context: Any) -> Any:
        del context
        submission_id = str(request.resource_id)
        if not submission_id:
            return self._error(
                request.meta,
                "invalid_argument",
                "submission_id is required",
            )
        try:
            outcome = self._api().submission_outcome(submission_id)
        except KeyError:
            return self._query(
                request.meta,
                {
                    "found": False,
                    "submission_id": submission_id,
                },
            )
        return self._query(
            request.meta,
            {
                "found": True,
                "submission": outcome,
            },
        )

    async def SubmitWorkflow(self, request: Any, context: Any) -> Any:
        del context
        try:
            meta = self._validate_meta(request.meta, write=True)
            payload = bytes(request.serialized_payload)
            if len(payload) > self.owner.max_inline_control_bytes:
                raise ContractValidationError(
                    "SubmitWorkflow payload exceeds max_inline_control_bytes"
                )
            digest = hashlib.sha256(payload).hexdigest()
            if not hmac.compare_digest(digest, str(request.serialized_payload_sha256)):
                raise ContractValidationError("SubmitWorkflow payload digest mismatch")
            decoded = cloudpickle.loads(payload)
            if not isinstance(decoded, SubmitRequest):
                raise ContractValidationError("SubmitWorkflow payload type is invalid")
            if decoded.contract.submission_id != str(request.submission_id):
                raise ContractValidationError("SubmitWorkflow submission_id mismatch")
            if decoded.contract.config_fingerprint != self._api().config_fingerprint:
                raise StateTransitionError("SubmitWorkflow config fingerprint changed")
            outcome = await self._api().submit(decoded)
            if outcome.run_id is not None:
                self._api().record_control_request(
                    outcome.run_id,
                    request_id=meta.request_id,
                    operation="submit_workflow",
                )
            return self._response(meta, outcome)
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def GetClusterSnapshot(self, request: Any, context: Any) -> Any:
        del context
        api = self._api()
        snapshot = api.cluster_snapshot()
        response_version = api.cluster_snapshot_version()
        kind = str(request.filter or "resources")
        if kind not in {"status", "nodes", "resources", "queues", "workers"}:
            return self._error(
                request.meta, "invalid_argument", "unknown cluster snapshot kind"
            )
        payload: object
        if kind == "status":
            runs = api.list_runs()
            started_at = getattr(api, "_started_wall_time_ms", None)
            payload = {
                "meta": api.snapshot_meta(snapshot_version=response_version),
                "kind": kind,
                "lifecycle_state": api.lifecycle_state,
                "started_wall_time_ms": started_at,
                "uptime_ms": (
                    None
                    if started_at is None
                    else max(0, api.clock.wall_ms() - started_at)
                ),
                "run_count": len(runs),
                "nonterminal_run_count": sum(not item.terminal for item in runs),
                "components": self._component_health_payload(api, snapshot),
            }
        elif kind == "queues":
            queue = api.queue_snapshot()
            response_version = int(queue.snapshot_version)
            payload = {
                "meta": api.snapshot_meta(snapshot_version=response_version),
                "policy": queue.policy_name,
                "policy_version": queue.policy_version,
                "partitioner": queue.partitioner_name,
                "tasks": queue.tasks,
            }
        elif kind == "workers":
            payload = self._worker_pools_payload(api, snapshot_version=response_version)
        else:
            payload = {
                "meta": api.snapshot_meta(snapshot_version=response_version),
                "kind": kind,
                "cluster": snapshot,
            }
        return self._query(
            request.meta,
            payload,
            snapshot_version=response_version,
        )

    @classmethod
    def _component_health_payload(cls, api: Any, cluster: Any) -> object:
        recorder = api.recorder.status()
        worker_payload = cls._worker_pool_snapshot(api)
        worker_failures = sum(
            int(getattr(worker_payload, name, 0))
            for name in (
                "replenish_failures",
                "reservation_failures",
                "sanitize_failures",
                "termination_failures",
            )
        )
        instances = () if api.inference is None else api.inference.model_instances()
        failed_instances = sum(
            str(getattr(item.state, "value", item.state)) == "failed"
            for item in instances
        )
        running = bool(api.core.running)
        lifecycle = str(getattr(api.lifecycle_state, "value", api.lifecycle_state))
        return {
            "controller": {
                "status": lifecycle,
                "healthy": lifecycle in {"ready", "draining"},
            },
            "ray_runtime": {
                "status": "ready" if running else "stopped",
                "healthy": running,
                "backend": type(api.runtime).__name__,
            },
            "recorder": {
                "status": (
                    "stopped"
                    if recorder.closed
                    else "degraded"
                    if recorder.writer_error_count
                    or recorder.dropped_control_event_count
                    or recorder.dropped_telemetry_count
                    or recorder.sequence_gap_count
                    else "ready"
                ),
                "healthy": not recorder.closed
                and recorder.writer_error_count == 0
                and recorder.dropped_control_event_count == 0
                and recorder.dropped_telemetry_count == 0
                and recorder.sequence_gap_count == 0,
                "snapshot": recorder,
            },
            "placement": {
                "status": "ready",
                "healthy": True,
                "snapshot_version": cluster.snapshot_version,
                "healthy_node_count": sum(
                    str(getattr(node.status, "value", node.status)) == "healthy"
                    for node in cluster.nodes
                ),
                "active_lease_count": cluster.active_lease_count,
            },
            "scheduler": {
                "status": "ready" if running else "stopped",
                "healthy": running,
                "policy": api.core.policy.name,
                "partitioner": api.core.partitioner.name,
            },
            "worker_pool": {
                "status": (
                    "disabled"
                    if worker_payload is None
                    else "degraded"
                    if worker_failures
                    else "ready"
                ),
                "healthy": worker_failures == 0,
                "snapshot": worker_payload,
            },
            "inference": {
                "status": (
                    "disabled"
                    if api.inference is None
                    else "degraded"
                    if failed_instances
                    else "ready"
                ),
                "healthy": failed_instances == 0,
                "instance_count": len(instances),
                "failed_instance_count": failed_instances,
            },
        }

    async def WatchCluster(self, request: Any, context: Any) -> AsyncIterator[Any]:
        try:
            meta = self._validate_meta(request.meta)
            api = self._api()
            after = int(request.after_snapshot_version)
            limit = int(request.limit or 100)
            if limit < 1:
                raise ContractValidationError("cluster watch limit must be positive")
            emitted = 0
            while True:
                current = int(api.cluster_snapshot_version())
                if current < after:
                    raise StateTransitionError(
                        "cluster snapshot generation changed; fetch a fresh snapshot"
                    )
                if current > after:
                    snapshot_required = current > after + 1
                    payload = {
                        "meta": api.snapshot_meta(snapshot_version=current),
                        "events": (
                            ()
                            if snapshot_required
                            else (
                                {
                                    "event_type": "cluster_snapshot_changed",
                                    "snapshot_version": current,
                                },
                            )
                        ),
                        "next_snapshot_version": current,
                        "snapshot_required": snapshot_required,
                    }
                    yield self._response(
                        meta,
                        payload,
                        snapshot_version=current,
                    )
                    emitted += 1
                    if snapshot_required or emitted >= limit:
                        return
                    after = current
                    continue
                remaining = context.time_remaining()
                if remaining is not None and remaining <= 0:
                    return
                await asyncio.sleep(0.05 if remaining is None else min(0.05, remaining))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield self._exception(request.meta, exc)

    async def GetWorkerPools(self, request: Any, context: Any) -> Any:
        del context
        try:
            api = self._api()
            snapshot_version = api.cluster_snapshot_version()
            return self._query(
                request.meta,
                self._worker_pools_payload(api, snapshot_version=snapshot_version),
                snapshot_version=snapshot_version,
            )
        except Exception as exc:
            return self._exception(request.meta, exc)

    @staticmethod
    def _worker_pool_snapshot(api: Any) -> object | None:
        broker = getattr(api, "worker_broker", None)
        if broker is None:
            return None
        snapshot_provider = getattr(broker, "snapshot", None)
        if callable(snapshot_provider):
            return snapshot_provider()
        return {
            "mode": "cold",
            "active_worker_lease_count": int(broker.active_count()),
        }

    @classmethod
    def _worker_pools_payload(cls, api: Any, *, snapshot_version: int) -> object:
        pool = cls._worker_pool_snapshot(api)
        if pool is None:
            pool = {
                "mode": "disabled",
                "active_worker_lease_count": 0,
                "workers": (),
                "worker_leases": (),
            }
        return {
            "meta": api.snapshot_meta(snapshot_version=snapshot_version),
            "worker_pool": pool,
            "worker_pools": (pool,),
        }

    async def ListRuns(self, request: Any, context: Any) -> Any:
        del context
        api = self._api()
        status_filter = str(request.filter or "")
        runs = tuple(
            item
            for item in api.list_runs()
            if not status_filter or item.status.value == status_filter
        )
        return self._query(
            request.meta,
            {
                "meta": api.snapshot_meta(
                    snapshot_version=api.control_events.latest_sequence
                ),
                "runs": runs,
            },
            snapshot_version=api.control_events.latest_sequence,
        )

    async def GetRun(self, request: Any, context: Any) -> Any:
        del context
        try:
            api = self._api()
            run = api.snapshot(str(request.resource_id))
            recording = api.run_recording_result(run.run_id)
            leases = tuple(
                item
                for item in api.placement.lease_snapshots()
                if item.lease.run_id == run.run_id
            )
            task_timing_snapshot = getattr(api.runtime, "task_timing_records", None)
            runtime_task_timings = (
                task_timing_snapshot(run.run_id)
                if callable(task_timing_snapshot)
                else ()
            )
            payload = {
                "meta": api.snapshot_meta(
                    snapshot_version=api.control_events.latest_sequence
                ),
                "run": run,
                "placements": leases,
                "runtime_task_timings": runtime_task_timings,
                "recording_complete": (
                    None if recording is None else recording.recording_complete
                ),
                "flush_result": recording,
            }
            return self._query(
                request.meta,
                payload,
                snapshot_version=api.control_events.latest_sequence,
            )
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def WatchRun(self, request: Any, context: Any) -> AsyncIterator[Any]:
        try:
            meta = self._validate_meta(request.meta)
            api = self._api()
            after = int(request.after_sequence)
            limit = int(request.limit or 100)
            while True:
                timeout = context.time_remaining()
                batch = await api.wait_run_events(
                    str(request.run_id),
                    after_sequence=after,
                    limit=limit,
                    timeout_seconds=timeout,
                )
                yield self._response(meta, batch, snapshot_version=batch.next_sequence)
                if batch.snapshot_required or batch.run_terminal:
                    return
                after = batch.next_sequence
        except Exception as exc:
            yield self._exception(request.meta, exc)

    async def GetRunEvents(self, request: Any, context: Any) -> Any:
        del context
        try:
            page = self._api().get_run_events(
                str(request.run_id),
                cursor=(str(request.cursor) or None),
                limit=int(request.limit or 100),
            )
            return self._query(request.meta, page)
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def CancelRun(self, request: Any, context: Any) -> Any:
        del context
        return await self._run_action(request, "cancel_run")

    async def DestroyRun(self, request: Any, context: Any) -> Any:
        del context
        return await self._run_action(request, "destroy_run")

    async def FlushRun(self, request: Any, context: Any) -> Any:
        del context
        return await self._run_action(request, "flush_run")

    async def GetTaskResultHandles(self, request: Any, context: Any) -> Any:
        del context
        try:
            handles = self._api().task_result_handles(
                str(request.resource_id), str(request.filter)
            )
            return self._query(request.meta, {"handles": handles})
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def GetModelCatalog(self, request: Any, context: Any) -> Any:
        del context
        api = self._api()
        if api.inference is None:
            return self._query(request.meta, {"catalog_revision": None, "models": ()})
        return self._query(
            request.meta,
            {
                "catalog_revision": api.inference.catalog.catalog_revision,
                "content_digest": api.inference.catalog.content_digest,
                "models": api.inference.catalog.specs,
            },
        )

    async def GetModelInstances(self, request: Any, context: Any) -> Any:
        del context
        api = self._api()
        instances = ()
        if api.inference is not None:
            instances = api.inference.instances.instances(
                model_id=(str(request.filter) or None)
            )
        return self._query(request.meta, {"instances": instances})

    async def WaitModelReady(self, request: Any, context: Any) -> Any:
        try:
            meta = self._validate_meta(request.meta)
            api = self._api()
            if api.inference is None:
                raise StateTransitionError("model inference is disabled")
            replicas = int(request.limit or 1)
            instances = await api.inference.wait_ready(
                str(request.resource_id),
                replicas=replicas,
                timeout_seconds=context.time_remaining(),
            )
            return self._response(meta, {"instances": instances})
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def GetRecorderStatus(self, request: Any, context: Any) -> Any:
        del context
        return self._query(request.meta, self._api().recorder.status())

    async def DrainNode(self, request: Any, context: Any) -> Any:
        del context
        return await self._node_action(request, "drain_node")

    async def ResumeNode(self, request: Any, context: Any) -> Any:
        del context
        return await self._node_action(request, "resume_node")

    async def ShutdownController(self, request: Any, context: Any) -> Any:
        del context
        try:
            meta = self._validate_meta(request.meta, write=True)
            payload = {
                "force": bool(request.force),
                "drain_timeout_ms": int(request.drain_timeout_ms),
            }
            digest = canonical_digest(payload)
            async with self.owner.write_lock:
                cached = self._api().request_journal.lookup(
                    meta.request_id,
                    operation="shutdown_controller",
                    payload_digest=digest,
                )
                if cached is None:
                    for run_id in self._api().core.recordable_run_ids():
                        self._api().record_control_request(
                            run_id,
                            request_id=meta.request_id,
                            operation="shutdown_controller",
                        )
                    if hasattr(self._api(), "_defer_local_rpc_close"):
                        self._api()._defer_local_rpc_close = True
                    result = await self._api().close(
                        force=bool(request.force),
                        drain_timeout_ms=int(request.drain_timeout_ms),
                    )
                    cached = self._api().request_journal.remember(
                        meta.request_id,
                        operation="shutdown_controller",
                        payload_digest=digest,
                        result=result,
                    )
            return self._response(meta, cached)
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def _run_action(self, request: Any, operation: str) -> Any:
        try:
            meta = self._validate_meta(request.meta, write=True)
            payload = {
                "run_id": str(request.run_id),
                "reason": str(request.reason),
                "force": bool(request.force),
            }
            digest = canonical_digest(payload)
            async with self.owner.write_lock:
                cached = self._api().request_journal.lookup(
                    meta.request_id,
                    operation=operation,
                    payload_digest=digest,
                )
                if cached is None:
                    self._api().record_control_request(
                        payload["run_id"],
                        request_id=meta.request_id,
                        operation=operation,
                    )
                    if operation == "cancel_run":
                        result = await self._api().cancel_run(
                            payload["run_id"],
                            reason=payload["reason"] or "user_cancelled",
                        )
                    elif operation == "destroy_run":
                        result = await self._api().destroy_run(
                            payload["run_id"], force=payload["force"]
                        )
                    else:
                        result = await self._api().flush_run(payload["run_id"])
                    cached = self._api().request_journal.remember(
                        meta.request_id,
                        operation=operation,
                        payload_digest=digest,
                        result=result,
                    )
            return self._response(meta, cached)
        except Exception as exc:
            return self._exception(request.meta, exc)

    async def _node_action(self, request: Any, operation: str) -> Any:
        try:
            meta = self._validate_meta(request.meta, write=True)
            payload = {
                "node_id": str(request.node_id),
                "boot_id": str(request.boot_id),
                "force": bool(request.force),
            }
            digest = canonical_digest(payload)
            async with self.owner.write_lock:
                cached = self._api().request_journal.lookup(
                    meta.request_id,
                    operation=operation,
                    payload_digest=digest,
                )
                if cached is None:
                    if operation == "drain_node":
                        result = await self._api().drain_node(
                            payload["node_id"],
                            boot_id=payload["boot_id"] or None,
                            force=payload["force"],
                            timeout_ms=max(0, meta.deadline_ms - 100),
                        )
                    else:
                        if payload["force"]:
                            raise ValueError("node resume does not support force")
                        result = await self._api().resume_node(
                            payload["node_id"], boot_id=payload["boot_id"] or None
                        )
                    cached = self._api().request_journal.remember(
                        meta.request_id,
                        operation=operation,
                        payload_digest=digest,
                        result=result,
                    )
            return self._response(meta, cached)
        except Exception as exc:
            return self._exception(request.meta, exc)

    def _query(
        self,
        meta_message: Any,
        payload: object,
        *,
        snapshot_version: int = 0,
    ) -> Any:
        try:
            meta = self._validate_meta(meta_message)
            return self._response(meta, payload, snapshot_version=snapshot_version)
        except Exception as exc:
            return self._exception(meta_message, exc)

    def _validate_meta(self, message: Any, *, write: bool = False) -> _RequestMeta:
        meta = _RequestMeta(
            schema_version=int(message.schema_version),
            request_id=str(message.request_id),
            client_version=str(message.client_version),
            config_fingerprint=str(message.config_fingerprint) or None,
            deadline_ms=int(message.deadline_ms),
            controller_generation=str(message.controller_generation) or None,
        )
        api = self._api()
        if (
            meta.config_fingerprint is not None
            and meta.config_fingerprint != api.config_fingerprint
        ):
            raise StateTransitionError("config fingerprint changed")
        if write and meta.controller_generation != api.controller_generation:
            raise StateTransitionError(
                "Controller generation changed; reconnect before writing"
            )
        return meta

    def _response(
        self,
        meta: _RequestMeta,
        payload: object,
        *,
        snapshot_version: int = 0,
    ) -> Any:
        return control_pb2.ControlResponseMessage(
            schema_version=1,
            request_id=meta.request_id,
            controller_generation=self._api().controller_generation,
            status_code="ok",
            error_code="",
            message="",
            snapshot_version=snapshot_version,
            json_payload=_json_bytes(payload),
        )

    def _error(self, meta_message: Any, code: str, message: str) -> Any:
        return control_pb2.ControlResponseMessage(
            schema_version=1,
            request_id=str(getattr(meta_message, "request_id", "")),
            controller_generation=self._api().controller_generation,
            status_code="error",
            error_code=code,
            message=message,
        )

    def _exception(self, meta_message: Any, exc: Exception) -> Any:
        if isinstance(exc, KeyError):
            code = "not_found"
        elif isinstance(exc, (ContractValidationError, ValueError)):
            code = "invalid_argument"
        elif isinstance(exc, SubmissionConflictError):
            code = "request_conflict"
        elif isinstance(exc, (StateTransitionError, RunNotTerminalError, RuntimeError)):
            code = "state_rejected"
        else:
            code = "control_internal_error"
        return self._error(meta_message, code, f"{type(exc).__name__}: {exc}")

    def _api(self) -> Any:
        if self.owner.control_api is None:
            raise RuntimeError("ControlService operation is not configured")
        return self.owner.control_api


@dataclass(frozen=True, slots=True)
class _RequestMeta:
    schema_version: int
    request_id: str
    client_version: str
    config_fingerprint: str | None
    deadline_ms: int
    controller_generation: str | None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.request_id or not self.client_version:
            raise ContractValidationError("invalid control request envelope")
        if self.deadline_ms < 1:
            raise ContractValidationError(
                "control request deadline_ms must be positive"
            )


class LocalControlServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        status_provider: Callable[[], ControllerStatus],
        control_api: object | None = None,
        max_inline_control_bytes: int = 1_048_576,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("control socket path must be absolute")
        self.socket_path = socket_path
        self.status_provider = status_provider
        self.control_api = control_api
        if max_inline_control_bytes < 1:
            raise ValueError("max_inline_control_bytes must be positive")
        self.max_inline_control_bytes = max_inline_control_bytes
        self._server: grpc.aio.Server | None = None
        self._socket_inode: int | None = None
        self.write_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._server is not None:
            return
        parent = self.socket_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        if self.socket_path.exists():
            raise RuntimeError(f"control socket already exists: {self.socket_path}")
        server = grpc.aio.server()
        control_pb2_grpc.add_LocalControlServicer_to_server(
            _LocalControlServicer(self), server
        )
        if server.add_insecure_port(_grpc_uds_target(self.socket_path)) == 0:
            raise RuntimeError(f"failed to bind control socket: {self.socket_path}")
        await server.start()
        self._server = server
        try:
            info = self.socket_path.stat()
        except FileNotFoundError:
            await server.stop(0)
            self._server = None
            raise RuntimeError(
                "gRPC did not create the configured control socket"
            ) from None
        if not stat.S_ISSOCK(info.st_mode):
            await server.stop(0)
            self._server = None
            raise RuntimeError("configured control path is not a Unix socket")
        os.chmod(self.socket_path, 0o600)
        self._socket_inode = info.st_ino

    async def close(self, grace_seconds: float = 1.0) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        await server.stop(grace_seconds)
        try:
            info = self.socket_path.stat()
        except FileNotFoundError:
            return
        if info.st_ino == self._socket_inode and stat.S_ISSOCK(info.st_mode):
            self.socket_path.unlink()


class UdsRuntimeClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        client_version: str = __version__,
        data_store: DataStore | None = None,
        data_owner_generation: str | None = None,
        max_inline_control_bytes: int = 1_048_576,
        shared_filesystem_roots: tuple[str, ...] = (),
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("control socket path must be absolute")
        self.socket_path = socket_path
        self.client_version = client_version
        self.controller_generation: str | None = None
        self.config_fingerprint: str | None = None
        self.data_store = data_store
        self.data_owner_generation = data_owner_generation
        self.max_inline_control_bytes = max_inline_control_bytes
        self.shared_filesystem_roots = normalize_shared_filesystem_roots(
            shared_filesystem_roots
        )
        self._compatibility_verified = False
        self._prepared: dict[str, PreparedSubmission] = {}

    def close(self) -> None:
        """Detach a client-owned Ray connection without stopping its owner."""

        store = self.data_store
        if isinstance(store, RayDataStore):
            store.close(kill_owner=False)
        self.data_store = None
        self.data_owner_generation = None

    async def get_controller_status(
        self, *, timeout_seconds: float = 5.0
    ) -> ControllerStatus:
        request_id = new_id("control_request")
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            stub = control_pb2_grpc.LocalControlStub(channel)
            response = await stub.GetControllerStatus(
                control_pb2.GetControllerStatusRequest(
                    schema_version=1,
                    request_id=request_id,
                    client_version=self.client_version,
                    deadline_ms=max(1, int(timeout_seconds * 1_000)),
                ),
                timeout=timeout_seconds,
            )
        if response.request_id != request_id:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response request_id mismatch",
            )
        if response.status_code != "ok":
            raise ControlRpcError(
                "control_protocol_invalid",
                response.message or "invalid ControlService response",
            )
        try:
            status = ControllerStatus(
                controller_generation=str(response.controller_generation),
                build_revision=str(response.build_revision),
                environment_fingerprint=str(response.environment_fingerprint),
                healthy_node_count=int(response.healthy_node_count),
            )
        except (TypeError, ValueError) as exc:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService status response is invalid",
            ) from exc
        self.controller_generation = status.controller_generation
        return status

    async def query(
        self,
        operation: str,
        *,
        resource_id: str = "",
        filter: str = "",
        limit: int = 100,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        request_id = new_id("control_request")
        request = control_pb2.ControlQueryRequest(
            meta=self._meta(request_id, timeout_seconds, write=False),
            resource_id=resource_id,
            filter=filter,
            limit=limit,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            stub = control_pb2_grpc.LocalControlStub(channel)
            method = getattr(stub, operation)
            response = await method(request, timeout=timeout_seconds)
        return self._decode(response, request_id)

    async def verify_compatibility(self, *, timeout_seconds: float = 5.0) -> None:
        if self._compatibility_verified:
            return
        version = await self.query("GetVersion", timeout_seconds=timeout_seconds)
        expected = {
            "project": "Ascend-Maze",
            "config_schema_version": 1,
            "control_protocol_version": 1,
        }
        mismatches = {
            name: (version.get(name), value)
            for name, value in expected.items()
            if version.get(name) != value
        }
        if mismatches:
            raise ControlRpcError(
                "version_incompatible",
                f"ControlService version identity mismatch: {mismatches}",
            )
        self._compatibility_verified = True

    async def prepare_submission(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        session_key: str | None = None,
        run_deadline_ms: int | None = None,
        execution_options: dict[str, object] | None = None,
    ) -> PreparedSubmission:
        await self.verify_compatibility()
        if isinstance(workflow, Workflow):
            compiled = workflow._compiled or workflow.compile()
            callables_by_definition: dict[str, Callable[..., object]] = {}
            for draft in workflow._draft_tasks:
                definition_id = compiled.tasks[draft.task_id].definition_id
                callables_by_definition.setdefault(definition_id, draft.template.func)
        else:
            compiled = workflow
            callables_by_definition = {}
        if set(inputs) != set(compiled.workflow_inputs):
            missing = sorted(set(compiled.workflow_inputs) - set(inputs))
            extra = sorted(set(inputs) - set(compiled.workflow_inputs))
            raise ContractValidationError(
                f"workflow input mismatch; missing={missing}, extra={extra}"
            )
        await self._ensure_data_store()
        assert self.data_store is not None
        assert self.data_owner_generation is not None
        if self.config_fingerprint is None:
            system = await self.query("GetSystemSnapshot")
            self.config_fingerprint = str(
                _require_mapping(system.get("meta"), "snapshot meta")[
                    "config_fingerprint"
                ]
            )
        resolved_submission_id = submission_id or new_id("submission")
        options_value = execution_options or {}
        frozen_options = freeze_canonical(options_value)
        if not isinstance(frozen_options, FrozenMap):
            raise ContractValidationError("execution_options must be a mapping")
        options = SubmissionOptions(
            run_deadline_ms=run_deadline_ms,
            execution_options=frozen_options,
        )
        existing = self._prepared.get(resolved_submission_id)
        if existing is not None:
            signature = tuple(
                (name, self._source_identity(inputs[name]))
                for name in sorted(inputs)
            )
            old = existing.request
            if (
                old.compiled.workflow_fingerprint != compiled.workflow_fingerprint
                or existing.input_signature != signature
                or old.contract.session_key_hash != hash_session_key(session_key)
                or old.contract.options != options
                or old.contract.config_fingerprint != self.config_fingerprint
            ):
                raise SubmissionConflictError(
                    "local submission_id is already prepared with another payload"
                )
            return existing
        handles: list[tuple[str, DataHandle]] = []
        try:
            for name in sorted(inputs):
                value = inputs[name]
                if isinstance(value, SharedFileRef):
                    validate_shared_file_ref(value, self.shared_filesystem_roots)
                handle = await asyncio.to_thread(
                    self.data_store.put_staged_for_submission_input,
                    value,
                    self.data_owner_generation,
                )
                handles.append((name, handle))
        except Exception:
            for _, handle in handles:
                self.data_store.release(handle)
            raise
        signature = tuple(
            (name, self._source_identity(inputs[name]))
            for name in sorted(inputs)
        )
        identities = tuple(
            run_input_identity(name, inputs[name], handle) for name, handle in handles
        )
        contract = SubmissionContract.create(
            submission_id=resolved_submission_id,
            workflow_fingerprint=compiled.workflow_fingerprint,
            input_identities=identities,
            session_key_hash=hash_session_key(session_key),
            options=options,
            config_fingerprint=self.config_fingerprint,
        )
        prepared = PreparedSubmission(
            request=SubmitRequest(
                compiled=compiled,
                code_packages=build_code_packages(
                    compiled,
                    environment_fingerprint=await self._environment_fingerprint(),
                    callables_by_definition=callables_by_definition,
                ),
                workflow_inputs=tuple(handles),
                contract=contract,
            ),
            input_signature=signature,
        )
        self._prepared[resolved_submission_id] = prepared
        return prepared

    async def submit_prepared(
        self,
        prepared: PreparedSubmission,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.controller_generation is None:
            await self.get_controller_status(timeout_seconds=min(5.0, timeout_seconds))
        payload = cloudpickle.dumps(prepared.request, protocol=5)
        if len(payload) > self.max_inline_control_bytes:
            raise ContractValidationError(
                "SubmitWorkflow control payload exceeds max_inline_control_bytes"
            )
        request_id = new_id("control_request")
        payload_digest = hashlib.sha256(payload).hexdigest()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        transport_retries = 0
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("SubmitWorkflow retry deadline expired")
                request = control_pb2.SubmitWorkflowRequest(
                    meta=self._meta(request_id, remaining, write=True),
                    submission_id=prepared.request.contract.submission_id,
                    serialized_payload_sha256=payload_digest,
                    serialized_payload=payload,
                )
                try:
                    async with grpc.aio.insecure_channel(
                        _grpc_uds_target(self.socket_path)
                    ) as channel:
                        response = await control_pb2_grpc.LocalControlStub(
                            channel
                        ).SubmitWorkflow(request, timeout=remaining)
                    outcome = self._decode(response, request_id)
                    break
                except (
                    grpc.aio.AioRpcError,
                    asyncio.TimeoutError,
                    ConnectionError,
                    OSError,
                ) as exc:
                    if transport_retries >= 1:
                        if isinstance(exc, grpc.aio.AioRpcError):
                            _raise_submit_transport(exc)
                        raise
                    transport_retries += 1
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise
                    await self.get_controller_status(
                        timeout_seconds=min(5.0, remaining)
                    )
        except ControlRpcError:
            self._release_staged_inputs(prepared)
            self._prepared.pop(prepared.request.contract.submission_id, None)
            raise
        state = outcome.get("state")
        if state == SubmissionState.ABORTED.value or bool(outcome.get("replayed")):
            self._release_staged_inputs(prepared)
        self._prepared.pop(prepared.request.contract.submission_id, None)
        return outcome

    @property
    def prepared_submission_count(self) -> int:
        return len(self._prepared)

    async def submit(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        session_key: str | None = None,
        run_deadline_ms: int | None = None,
        execution_options: dict[str, object] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        prepared = await self.prepare_submission(
            workflow,
            inputs=inputs,
            submission_id=submission_id,
            session_key=session_key,
            run_deadline_ms=run_deadline_ms,
            execution_options=execution_options,
        )
        return await self.submit_prepared(prepared, timeout_seconds=timeout_seconds)

    async def get_submission_status(
        self,
        submission_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        if not isinstance(submission_id, str) or not submission_id:
            raise ValueError("submission_id is required")
        return await self.query(
            "GetSubmission",
            resource_id=submission_id,
            timeout_seconds=timeout_seconds,
        )

    async def run(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> str:
        outcome = await self.submit(
            workflow,
            inputs=inputs,
            submission_id=submission_id,
            timeout_seconds=timeout_seconds,
        )
        run_id = outcome.get("run_id")
        if outcome.get("state") == SubmissionState.ABORTED.value or not isinstance(
            run_id, str
        ):
            raise RuntimeError(str(outcome.get("error") or "submission aborted"))
        return run_id

    async def run_action(
        self,
        operation: str,
        run_id: str,
        *,
        reason: str = "",
        force: bool = False,
        request_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        resolved_id = request_id or new_id("control_request")
        request = control_pb2.RunActionRequest(
            meta=self._meta(resolved_id, timeout_seconds, write=True),
            run_id=run_id,
            reason=reason,
            force=force,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            stub = control_pb2_grpc.LocalControlStub(channel)
            response = await getattr(stub, operation)(request, timeout=timeout_seconds)
        return self._decode(response, resolved_id)

    async def get_run_events(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        request_id = new_id("control_request")
        request = control_pb2.GetRunEventsRequest(
            meta=self._meta(request_id, timeout_seconds, write=False),
            run_id=run_id,
            cursor=cursor or "",
            limit=limit,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            response = await control_pb2_grpc.LocalControlStub(channel).GetRunEvents(
                request, timeout=timeout_seconds
            )
        return self._decode(response, request_id)

    async def materialize_task_result(
        self,
        run_id: str,
        task_id: str,
    ) -> dict[str, object]:
        response = await self.query(
            "GetTaskResultHandles",
            resource_id=run_id,
            filter=task_id,
        )
        raw_handles = response.get("handles")
        if not isinstance(raw_handles, list):
            raise RuntimeError("Task result handle response is invalid")
        await self._ensure_data_store()
        assert self.data_store is not None
        result: dict[str, object] = {}
        for item in raw_handles:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
            ):
                raise RuntimeError("Task result handle entry is invalid")
            payload = _require_mapping(item[1], "DataHandle")
            metadata = freeze_canonical(_decode_json_value(payload.get("metadata", {})))
            if not isinstance(metadata, FrozenMap):
                raise RuntimeError("DataHandle.metadata is invalid")
            handle = DataHandle(
                owner_generation=_required_json_string(payload, "owner_generation"),
                staged_handle_id=_required_json_string(payload, "staged_handle_id"),
                stable_digest=(
                    None
                    if payload.get("stable_digest") is None
                    else _required_json_string(payload, "stable_digest")
                ),
                size_bytes=(
                    None
                    if payload.get("size_bytes") is None
                    else _required_json_int(payload, "size_bytes")
                ),
                metadata=metadata,
            )
            result[item[0]] = await asyncio.to_thread(self.data_store.get, handle)
        return result

    async def node_action(
        self,
        operation: str,
        node_id: str,
        *,
        boot_id: str = "",
        force: bool = False,
        request_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        resolved_id = request_id or new_id("control_request")
        request = control_pb2.NodeActionRequest(
            meta=self._meta(resolved_id, timeout_seconds, write=True),
            node_id=node_id,
            boot_id=boot_id,
            force=force,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            response = await getattr(
                control_pb2_grpc.LocalControlStub(channel), operation
            )(request, timeout=timeout_seconds)
        return self._decode(response, resolved_id)

    async def wait_model_ready(
        self,
        model_id: str,
        *,
        replicas: int = 1,
        timeout_seconds: float = 300.0,
    ) -> dict[str, object]:
        request_id = new_id("control_request")
        request = control_pb2.ControlQueryRequest(
            meta=self._meta(request_id, timeout_seconds, write=False),
            resource_id=model_id,
            limit=replicas,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            response = await control_pb2_grpc.LocalControlStub(channel).WaitModelReady(
                request, timeout=timeout_seconds
            )
        return self._decode(response, request_id)

    async def shutdown_controller(
        self,
        *,
        force: bool = False,
        drain_timeout_ms: int = 5_000,
        request_id: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        resolved_id = request_id or new_id("control_request")
        request = control_pb2.ShutdownControllerRequest(
            meta=self._meta(resolved_id, timeout_seconds, write=True),
            force=force,
            drain_timeout_ms=drain_timeout_ms,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            response = await control_pb2_grpc.LocalControlStub(
                channel
            ).ShutdownController(request, timeout=timeout_seconds)
        return self._decode(response, resolved_id)

    async def watch_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        loop = asyncio.get_running_loop()
        expires_at = None if timeout_seconds is None else loop.time() + timeout_seconds
        after = after_sequence
        while True:
            remaining = None if expires_at is None else expires_at - loop.time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("run watch deadline expired")
            request_id = new_id("control_request")
            request_deadline = 86_400.0 if remaining is None else remaining
            request = control_pb2.WatchRunRequest(
                meta=self._meta(request_id, request_deadline, write=False),
                run_id=run_id,
                after_sequence=after,
                limit=limit,
            )
            reconnect = False
            async with grpc.aio.insecure_channel(
                _grpc_uds_target(self.socket_path)
            ) as channel:
                call = control_pb2_grpc.LocalControlStub(channel).WatchRun(
                    request,
                    timeout=remaining,
                )
                async for response in call:
                    batch = self._decode(response, request_id)
                    yield batch
                    next_sequence = batch.get("next_sequence")
                    if (
                        isinstance(next_sequence, bool)
                        or not isinstance(next_sequence, int)
                        or next_sequence < after
                    ):
                        raise ControlRpcError(
                            "control_protocol_invalid",
                            "ControlService watch sequence is invalid",
                        )
                    after = next_sequence
                    if batch.get("run_terminal") is True:
                        return
                    if batch.get("snapshot_required") is True:
                        shown = await self.query(
                            "GetRun",
                            resource_id=run_id,
                            timeout_seconds=(
                                5.0 if remaining is None else max(0.001, remaining)
                            ),
                        )
                        run = _require_mapping(shown.get("run"), "run")
                        if str(run.get("status")) in {
                            "succeeded",
                            "failed",
                            "cancelled",
                            "timed_out",
                            "interrupted",
                        }:
                            # Reconnect once so callers observe the authoritative
                            # terminal watch batch instead of a locally fabricated
                            # event after the snapshot/watch race.
                            reconnect = True
                            break
                        reconnect = True
                        break
            if not reconnect:
                shown = await self.query(
                    "GetRun",
                    resource_id=run_id,
                    timeout_seconds=(
                        5.0 if remaining is None else max(0.001, remaining)
                    ),
                )
                run = _require_mapping(shown.get("run"), "run")
                if str(run.get("status")) in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "timed_out",
                    "interrupted",
                }:
                    reconnect = True
            await asyncio.sleep(0)

    async def watch_cluster(
        self,
        *,
        after_snapshot_version: int = 0,
        limit: int = 100,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        request_id = new_id("control_request")
        deadline = 86_400.0 if timeout_seconds is None else timeout_seconds
        request = control_pb2.WatchClusterRequest(
            meta=self._meta(request_id, deadline, write=False),
            after_snapshot_version=after_snapshot_version,
            limit=limit,
        )
        async with grpc.aio.insecure_channel(
            _grpc_uds_target(self.socket_path)
        ) as channel:
            call = control_pb2_grpc.LocalControlStub(channel).WatchCluster(
                request,
                timeout=timeout_seconds,
            )
            async for response in call:
                yield self._decode(response, request_id)

    def _meta(self, request_id: str, timeout_seconds: float, *, write: bool) -> Any:
        return control_pb2.ControlRequestMetaMessage(
            schema_version=1,
            request_id=request_id,
            client_version=self.client_version,
            config_fingerprint=self.config_fingerprint or "",
            deadline_ms=max(1, int(timeout_seconds * 1_000)),
            controller_generation=(self.controller_generation or "") if write else "",
        )

    def _decode(self, response: Any, request_id: str) -> dict[str, object]:
        try:
            schema_version = int(response.schema_version)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response schema is invalid",
            ) from exc
        if schema_version != 1:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response schema is incompatible",
            )
        if str(response.request_id) != request_id:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response request_id mismatch",
            )
        controller_generation = str(response.controller_generation)
        if not controller_generation:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response Controller generation is missing",
            )
        if str(response.status_code) != "ok":
            error_code = str(response.error_code)
            if not error_code:
                raise ControlRpcError(
                    "control_protocol_invalid",
                    "ControlService error response has no error code",
                )
            raise ControlRpcError(error_code, str(response.message))
        try:
            payload = json.loads(bytes(response.json_payload) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlRpcError(
                "control_protocol_invalid",
                "ControlService response JSON is invalid",
            ) from exc
        self.controller_generation = controller_generation
        if not isinstance(payload, dict):
            return {"value": payload}
        return payload

    async def _ensure_data_store(self) -> None:
        if self.data_store is not None and self.data_owner_generation is not None:
            return
        system = await self.query("GetSystemSnapshot")
        self.config_fingerprint = str(
            _require_mapping(system.get("meta"), "snapshot meta")["config_fingerprint"]
        )
        descriptor_value = system.get("data_store_descriptor")
        descriptor = _require_mapping(descriptor_value, "data_store_descriptor")
        owner_name = descriptor.get("owner_actor_name")
        namespace = descriptor.get("owner_namespace")
        generation = descriptor.get("owner_generation")
        if not all(
            isinstance(value, str) and value
            for value in (owner_name, namespace, generation)
        ):
            raise RuntimeError("Controller did not expose a RayDataStore descriptor")
        owner_name = cast(str, owner_name)
        namespace = cast(str, namespace)
        generation = cast(str, generation)
        ray_descriptor = RayDataStoreDescriptor(owner_name, namespace, generation)
        self.data_store = RayDataStore.connect_client(ray_descriptor)
        self.data_owner_generation = generation

    async def _environment_fingerprint(self) -> str:
        status = await self.get_controller_status()
        return status.environment_fingerprint

    def _release_staged_inputs(self, prepared: PreparedSubmission) -> None:
        if self.data_store is None:
            return
        for _, handle in prepared.request.workflow_inputs:
            try:
                if self.data_store.state_of(handle) == "staged":
                    self.data_store.release(handle)
            except Exception:
                pass

    @staticmethod
    def _source_identity(value: object) -> tuple[str, ...]:
        if isinstance(value, SharedFileRef):
            return (
                "shared_file",
                value.canonical_path,
                value.content_sha256,
                str(value.size_bytes),
            )
        return (
            "object",
            type(value).__module__,
            type(value).__qualname__,
            str(id(value)),
        )


class ControlRpcError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"control payload is not JSON-compatible: {type(value).__name__}")


def _grpc_uds_target(path: Path) -> str:
    return f"unix:{path}"


def _raise_submit_transport(exc: grpc.aio.AioRpcError) -> NoReturn:
    if exc.code() is grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError("SubmitWorkflow RPC deadline exceeded") from exc
    raise ConnectionError(
        f"SubmitWorkflow RPC transport failed: {exc.code().name}"
    ) from exc


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlRpcError(
            "control_protocol_invalid",
            f"{name} is not a JSON object",
        )
    return value


def _required_json_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ControlRpcError(
            "control_protocol_invalid",
            f"DataHandle.{name} is invalid",
        )
    return value


def _required_json_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlRpcError(
            "control_protocol_invalid",
            f"DataHandle.{name} is invalid",
        )
    return value


def _decode_json_value(value: object) -> object:
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"encoding", "value"} and value.get("encoding") == "base64":
            encoded = value.get("value")
            if not isinstance(encoded, str):
                raise ControlRpcError(
                    "control_protocol_invalid",
                    "base64 control value is invalid",
                )
            try:
                return b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as exc:
                raise ControlRpcError(
                    "control_protocol_invalid",
                    "base64 control value is invalid",
                ) from exc
        return {str(key): _decode_json_value(item) for key, item in value.items()}
    return value
