"""Backend-neutral task dispatch contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol, runtime_checkable

from ascend_maze.contracts.data import DataHandle
from ascend_maze.contracts.resources import ExecutionTarget, PlacementLease
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import CanonicalizationError, ContractValidationError


@dataclass(frozen=True, slots=True)
class CodePackage:
    definition_id: str
    code_hash: str
    module: str
    qualname: str
    serialized_fallback: bytes | None
    serialized_payload_digest: str | None
    environment_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "definition_id",
            "code_hash",
            "module",
            "qualname",
            "environment_fingerprint",
        ):
            if not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if self.serialized_fallback is None:
            if self.serialized_payload_digest is not None:
                raise ContractValidationError(
                    "serialized payload digest requires serialized fallback bytes"
                )
            return
        expected = hashlib.sha256(self.serialized_fallback).hexdigest()
        if self.serialized_payload_digest != expected:
            raise ContractValidationError("serialized payload digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        definition_id: str,
        code_hash: str,
        module: str,
        qualname: str,
        serialized_fallback: bytes | None,
        environment_fingerprint: str,
    ) -> "CodePackage":
        digest = (
            None
            if serialized_fallback is None
            else hashlib.sha256(serialized_fallback).hexdigest()
        )
        return cls(
            definition_id=definition_id,
            code_hash=code_hash,
            module=module,
            qualname=qualname,
            serialized_fallback=serialized_fallback,
            serialized_payload_digest=digest,
            environment_fingerprint=environment_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class CodeHandle:
    code_handle_id: str
    definition_id: str
    code_hash: str

    def __post_init__(self) -> None:
        for name in ("code_handle_id", "definition_id", "code_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class RuntimeArgument:
    name: str
    kind: str
    literal: CanonicalValue | None = None
    data_handle: DataHandle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ContractValidationError("runtime argument name is required")
        if self.kind not in {"literal", "data_handle", "default_omitted"}:
            raise ContractValidationError(f"unsupported runtime argument kind: {self.kind}")
        if self.kind == "literal":
            if self.data_handle is not None:
                raise ContractValidationError("literal argument cannot carry DataHandle")
            try:
                object.__setattr__(self, "literal", freeze_canonical(self.literal))
            except CanonicalizationError as exc:
                raise ContractValidationError(
                    "runtime literal must be a canonical value"
                ) from exc
        if self.kind == "data_handle":
            if not isinstance(self.data_handle, DataHandle):
                raise ContractValidationError("data_handle argument requires DataHandle")
            if self.literal is not None:
                raise ContractValidationError(
                    "data_handle argument cannot carry a literal"
                )
        if self.kind == "default_omitted" and (
            self.literal is not None or self.data_handle is not None
        ):
            raise ContractValidationError(
                "default_omitted argument cannot carry a value"
            )


@dataclass(frozen=True, slots=True)
class ModelRouteLease:
    route_lease_id: str
    run_id: str
    task_id: str
    attempt: int
    model_id: str
    catalog_revision: str
    instance_id: str
    instance_generation: int
    adapter_name: str
    endpoint_id: str
    instance_node_id: str
    instance_boot_id: str
    affinity_key_hash: str
    created_at_ms: int
    dispatch_deadline_ms: int

    def __post_init__(self) -> None:
        for name in (
            "route_lease_id",
            "run_id",
            "task_id",
            "model_id",
            "catalog_revision",
            "instance_id",
            "adapter_name",
            "endpoint_id",
            "instance_node_id",
            "instance_boot_id",
            "affinity_key_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        for name in ("attempt", "instance_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"{name} must be positive")
        for name in ("created_at_ms", "dispatch_deadline_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if self.dispatch_deadline_ms <= self.created_at_ms:
            raise ContractValidationError(
                "route dispatch deadline must follow creation time"
            )


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    dispatch_id: str
    run_id: str
    task_id: str
    attempt: int
    task_kind: str
    execution_target: ExecutionTarget
    model_route: ModelRouteLease | None
    code_handle: CodeHandle
    arguments: tuple[RuntimeArgument, ...]
    expected_outputs: tuple[str, ...]
    timeout_ms: int | None
    environment_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("dispatch_id", "run_id", "task_id", "environment_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ContractValidationError("attempt must be a positive integer")
        if self.task_kind not in {"cpu", "npu", "io"}:
            raise ContractValidationError("unsupported task_kind")
        if not isinstance(self.execution_target, ExecutionTarget):
            raise ContractValidationError("execution_target must be ExecutionTarget")
        if not isinstance(self.code_handle, CodeHandle):
            raise ContractValidationError("code_handle must be CodeHandle")
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(item, RuntimeArgument) for item in self.arguments
        ):
            raise ContractValidationError("arguments must be RuntimeArgument tuple")
        argument_names = [item.name for item in self.arguments]
        if len(argument_names) != len(set(argument_names)):
            raise ContractValidationError("runtime argument names must be unique")
        if (
            not isinstance(self.expected_outputs, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.expected_outputs
            )
            or len(self.expected_outputs) != len(set(self.expected_outputs))
        ):
            raise ContractValidationError("expected_outputs must contain unique names")
        if self.timeout_ms is not None and (
            isinstance(self.timeout_ms, bool)
            or not isinstance(self.timeout_ms, int)
            or self.timeout_ms <= 0
        ):
            raise ContractValidationError("timeout_ms must be positive or None")
        if self.execution_target is ExecutionTarget.MODEL_SERVICE:
            if self.model_route is None:
                raise ContractValidationError(
                    "model service request requires ModelRouteLease"
                )
            if (
                self.model_route.run_id != self.run_id
                or self.model_route.task_id != self.task_id
                or self.model_route.attempt != self.attempt
            ):
                raise ContractValidationError(
                    "ModelRouteLease does not match execution Attempt"
                )
        elif self.model_route is not None:
            raise ContractValidationError(
                "local worker request cannot carry ModelRouteLease"
            )


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    dispatch_id: str
    backend_name: str
    run_id: str
    task_id: str
    attempt: int
    lease_id: str
    route_lease_id: str | None
    worker_endpoint_id: str

    def __post_init__(self) -> None:
        for name in (
            "dispatch_id",
            "backend_name",
            "run_id",
            "task_id",
            "lease_id",
            "worker_endpoint_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ContractValidationError("attempt must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class RuntimeDeviceMapping:
    physical_device_id: str
    runtime_visible_device_id: str
    visible_device_index: int = 0

    def __post_init__(self) -> None:
        for name in ("physical_device_id", "runtime_visible_device_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.visible_device_index, bool)
            or not isinstance(self.visible_device_index, int)
            or self.visible_device_index < 0
        ):
            raise ContractValidationError(
                "visible_device_index must be a non-negative integer"
            )

    @classmethod
    def identity(cls, physical_device_id: str) -> "RuntimeDeviceMapping":
        return cls(
            physical_device_id=physical_device_id,
            runtime_visible_device_id=physical_device_id,
            visible_device_index=0,
        )


@dataclass(frozen=True, slots=True)
class RuntimeNodeBinding:
    node_id: str
    boot_id: str
    ray_node_id: str
    runtime_generation: int
    agent_generation: str
    agent_endpoint: str
    producer_id: str
    records_locally: bool = False
    device_mappings: tuple[RuntimeDeviceMapping, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "boot_id",
            "ray_node_id",
            "agent_generation",
            "agent_endpoint",
            "producer_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.runtime_generation, bool)
            or not isinstance(self.runtime_generation, int)
            or self.runtime_generation < 1
        ):
            raise ContractValidationError("runtime_generation must be positive")
        if not isinstance(self.records_locally, bool):
            raise ContractValidationError("records_locally must be a boolean")
        if not isinstance(self.device_mappings, tuple) or any(
            not isinstance(item, RuntimeDeviceMapping)
            for item in self.device_mappings
        ):
            raise ContractValidationError(
                "device_mappings must contain RuntimeDeviceMapping values"
            )
        ordered = tuple(
            sorted(self.device_mappings, key=lambda item: item.physical_device_id)
        )
        physical_ids = tuple(item.physical_device_id for item in ordered)
        if len(physical_ids) != len(set(physical_ids)):
            raise ContractValidationError(
                "physical device IDs must be unique in RuntimeNodeBinding"
            )
        object.__setattr__(self, "device_mappings", ordered)

    def device_mapping(self, physical_device_id: str) -> RuntimeDeviceMapping:
        for mapping in self.device_mappings:
            if mapping.physical_device_id == physical_device_id:
                return mapping
        if not self.device_mappings:
            return RuntimeDeviceMapping.identity(physical_device_id)
        raise ContractValidationError(
            f"physical device {physical_device_id!r} is absent from node topology"
        )


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    lease_id: str
    node_id: str
    boot_id: str
    runtime_generation: int
    physical_device_id: str
    runtime_visible_device_id: str
    visible_device_index: int
    environment_variables: FrozenMap[str, str]

    def __post_init__(self) -> None:
        for name in (
            "lease_id",
            "node_id",
            "boot_id",
            "physical_device_id",
            "runtime_visible_device_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        if (
            isinstance(self.runtime_generation, bool)
            or not isinstance(self.runtime_generation, int)
            or self.runtime_generation < 1
        ):
            raise ContractValidationError("runtime_generation must be positive")
        if (
            isinstance(self.visible_device_index, bool)
            or not isinstance(self.visible_device_index, int)
            or self.visible_device_index < 0
        ):
            raise ContractValidationError(
                "visible_device_index must be a non-negative integer"
            )
        frozen = freeze_canonical(self.environment_variables)
        if not isinstance(frozen, FrozenMap) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in frozen.items_tuple()
        ):
            raise ContractValidationError(
                "environment_variables must map strings to strings"
            )
        visible = frozen.get("ASCEND_RT_VISIBLE_DEVICES")
        if visible != self.runtime_visible_device_id:
            raise ContractValidationError(
                "ASCEND_RT_VISIBLE_DEVICES must select the runtime-visible device"
            )
        object.__setattr__(self, "environment_variables", frozen)

    @classmethod
    def from_lease(
        cls,
        lease: PlacementLease,
        binding: RuntimeNodeBinding,
    ) -> "DeviceBinding":
        if lease.npu_device_id is None or lease.resources.npu_slots != 1:
            raise ContractValidationError(
                "local NPU DeviceBinding requires one leased NPU slot"
            )
        if lease.node_id != binding.node_id or lease.boot_id != binding.boot_id:
            raise ContractValidationError(
                "PlacementLease generation does not match RuntimeNodeBinding"
            )
        mapping = binding.device_mapping(lease.npu_device_id)
        return cls(
            lease_id=lease.lease_id,
            node_id=lease.node_id,
            boot_id=lease.boot_id,
            runtime_generation=binding.runtime_generation,
            physical_device_id=lease.npu_device_id,
            runtime_visible_device_id=mapping.runtime_visible_device_id,
            visible_device_index=mapping.visible_device_index,
            environment_variables=FrozenMap(
                (
                    (
                        "ASCEND_RT_VISIBLE_DEVICES",
                        mapping.runtime_visible_device_id,
                    ),
                )
            ),
        )


@runtime_checkable
class RuntimeBackend(Protocol):
    async def start(self) -> None: ...

    async def prepare(
        self, definitions: tuple[CodePackage, ...]
    ) -> tuple[CodeHandle, ...]: ...

    async def dispatch(
        self,
        request: ExecutionRequest,
        lease: PlacementLease,
    ) -> DispatchHandle: ...

    async def cancel(self, handle: DispatchHandle, reason: str) -> None: ...

    async def release_code(self, handles: tuple[CodeHandle, ...]) -> None: ...

    async def close(self) -> None: ...
