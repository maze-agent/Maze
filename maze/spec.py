"""Versioned JSON/YAML contracts. All frontends lower to the same Python graph."""

from copy import deepcopy
from importlib import import_module
from math import isfinite
from typing import Annotated, Any, Self
from typing import Literal as Choice

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from maze.graph import DAG, Input, Literal, Node, Output, compile

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    def document(self) -> dict[str, Any]:
        """Wire representation preserving omitted defaults versus explicit null."""
        return self.model_dump(mode="json", by_alias=True)


def check_json(value: Any, depth: int = 0) -> None:
    """Validate the shared JSON boundary without coercing Python objects or keys."""
    if depth > 32:
        raise ValueError("JSON data too deeply nested")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            check_json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            check_json(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError("Expected JSON data")
    elif type(value) is float and not isfinite(value):
        raise ValueError("JSON numbers must be finite")


def check_types(source: str, target: str) -> None:
    if source != target and "any" not in (source, target):
        if (source, target) != ("integer", "number"):
            raise ValueError("Incompatible port types")


class Port(Model):
    type: Choice[
        "any", "number", "integer", "string", "boolean", "array", "object", "null"
    ] = "any"
    default: Any = None

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = handler(self)
        if "default" not in self.model_fields_set:
            result.pop("default", None)
        return result

    @model_validator(mode="after")
    def check_default(self) -> Self:
        if "default" in self.model_fields_set:
            check_json(self.default)
            self.check(self.default)
        return self

    def check(self, value: Any) -> None:
        valid = {
            "any": True,
            "number": type(value) in (int, float),
            "integer": type(value) is int,
            "string": isinstance(value, str),
            "boolean": type(value) is bool,
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
            "null": value is None,
        }[self.type]
        if not valid:
            raise ValueError(f"Expected port type {self.type}")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("Numeric port values must be finite")


class RetryPolicy(Model):
    pure: bool = False
    max_retries: int = Field(default=0, ge=0, le=3)
    retry_exceptions: bool = False


class Resources(Model):
    num_cpus: float = Field(default=1, ge=0, allow_inf_nan=False)
    num_gpus: float = Field(default=0, ge=0, allow_inf_nan=False)
    memory_bytes: int | None = Field(default=None, gt=0)


class NodeSpec(Model):
    id: Name
    kind: Choice["python"] = "python"
    callable: str
    execution: Choice["task", "async_actor"] = "task"
    resources: Resources = Field(default_factory=Resources)
    max_concurrency: int = Field(default=8, ge=1, le=1024)
    inputs: dict[Name, Port] = Field(default_factory=dict)
    constants: dict[Name, Any] = Field(default_factory=dict)
    outputs: dict[Name, Port]
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def check_constants(self) -> Self:
        check_json(self.constants)
        return self


class Endpoint(Model):
    node: Name
    port: Name


class GraphInput(Model):
    input: Name


class Edge(Model):
    source: GraphInput | Endpoint = Field(alias="from")
    to: Endpoint


class FinalOutput(Port):
    source: Endpoint = Field(alias="from")


class DagSpec(Model):
    schema_version: Choice["maze.dag/v1"] = Field(alias="schema")
    name: Name
    inputs: dict[Name, Port] = Field(default_factory=dict)
    nodes: tuple[NodeSpec, ...] = Field(min_length=1, max_length=256)
    edges: tuple[Edge, ...] = ()
    outputs: dict[Name, FinalOutput]

    def to_dag(self) -> DAG:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Duplicate node IDs")
        for port in self.inputs.values():
            port.check_default()
        for port in self.outputs.values():
            parent = nodes.get(port.source.node)
            if parent is None or port.source.port not in parent.outputs:
                raise ValueError("Unknown final output")
            check_types(parent.outputs[port.source.port].type, port.type)
        bindings: dict[str, dict[str, Input | Output | Literal]] = {
            node.id: {} for node in self.nodes
        }
        for edge in self.edges:
            target = edge.to
            if target.node not in nodes or target.port not in nodes[target.node].inputs:
                raise ValueError("Unknown target input")
            if target.port in bindings[target.node]:
                raise ValueError("Multiple bindings for one input")
            if isinstance(edge.source, GraphInput):
                if edge.source.input not in self.inputs:
                    raise ValueError("Unknown graph input")
                source_type = self.inputs[edge.source.input].type
                source = Input(edge.source.input)
            else:
                parent = nodes.get(edge.source.node)
                if parent is None or edge.source.port not in parent.outputs:
                    raise ValueError("Unknown source output")
                source_type = parent.outputs[edge.source.port].type
                source = Output(edge.source.node, edge.source.port)
            target_type = nodes[target.node].inputs[target.port].type
            check_types(source_type, target_type)
            bindings[target.node][target.port] = source
        result = []
        for node in self.nodes:
            current = bindings[node.id]
            for port in (*node.inputs.values(), *node.outputs.values()):
                port.check_default()
            for name, value in node.constants.items():
                if name not in node.inputs or name in current:
                    raise ValueError("Invalid constant binding")
                check_json(value)
                node.inputs[name].check(value)
                current[name] = Literal(value)
            for name, port in node.inputs.items():
                if name not in current:
                    if "default" not in port.model_fields_set:
                        raise ValueError(f"Missing input binding: {node.id}.{name}")
                    port.check(port.default)
                    current[name] = Literal(port.default)
            module, separator, attribute = node.callable.partition(":")
            if not separator or not attribute.isidentifier():
                raise ValueError("Callable must be package.module:function")
            function = getattr(import_module(module), attribute)
            result.append(
                Node(
                    node.id,
                    function,
                    current,
                    tuple(node.outputs),
                    node.resources.num_cpus,
                    node.resources.num_gpus,
                    node.execution,
                    node.max_concurrency,
                    node.resources.memory_bytes,
                    {name: port.check for name, port in node.inputs.items()},
                    {name: port.check for name, port in node.outputs.items()},
                    node.retry.pure,
                    node.retry.max_retries,
                    node.retry.retry_exceptions,
                )
            )
        graph = DAG(
            tuple(self.inputs),
            tuple(result),
            {
                name: Output(port.source.node, port.source.port)
                for name, port in self.outputs.items()
            },
        )
        compile(graph)
        return graph

    def check_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        result = dict(values)
        if result.keys() - self.inputs.keys():
            raise ValueError("Unknown invocation inputs")
        for name, port in self.inputs.items():
            if name not in result and "default" in port.model_fields_set:
                result[name] = deepcopy(port.default)
            if name not in result:
                raise ValueError(f"Missing invocation input: {name}")
            port.check(result[name])
        return result

    def check_outputs(self, values: dict[str, Any]) -> None:
        for name, port in self.outputs.items():
            port.check(values[name])


class DeploymentSpec(Model):
    schema_version: Choice["maze.deployment/v1"] = Field(
        default="maze.deployment/v1", alias="schema"
    )
    max_active_invocations: int = Field(default=8, ge=1, le=1024)


class Limits(Model):
    max_active_invocations: int = Field(default=8, ge=1)
    max_waiting_invocations: int = Field(default=16, ge=0)
    max_inflight_node_calls: int = Field(default=2048, ge=1)
    max_nodes_per_dag: int = Field(default=256, ge=1, le=256)
    max_input_bytes: int = Field(default=1048576, ge=1)
    max_output_bytes: int = Field(default=8388608, ge=1)


class Timeouts(Model):
    default_invocation_s: float = Field(default=120, gt=0, allow_inf_nan=False)
    max_invocation_s: float = Field(default=600, gt=0, allow_inf_nan=False)
    deployment_s: float = Field(default=60, gt=0, allow_inf_nan=False)
    shutdown_grace_s: float = Field(default=10, gt=0, allow_inf_nan=False)
    cancellation_grace_s: float = Field(default=2, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_default(self) -> Self:
        if self.default_invocation_s > self.max_invocation_s:
            raise ValueError("Default deadline exceeds maximum")
        return self


class ClusterConfig(Model):
    mode: Choice["managed-local"] = "managed-local"
    num_cpus: int = Field(default=2, ge=1)
    object_store_memory: int = Field(default=256 * 1024**2, ge=80 * 1024**2)


class ApiConfig(Model):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    token_env: str = "MAZE_API_TOKEN"


class EngineConfig(Model):
    schema_version: Choice["maze.engine/v1"] = Field(
        default="maze.engine/v1", alias="schema"
    )
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    limits: Limits = Field(default_factory=Limits)
    timeouts: Timeouts = Field(default_factory=Timeouts)


class InvokeRequest(Model):
    input: dict[str, Any]
    invocation_id: Name | None = None
    timeout_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    metrics: Choice["none", "basic", "detailed"] = "basic"

    @model_validator(mode="after")
    def check_input(self) -> Self:
        check_json(self.input)
        return self


def parse_document(text: str, max_bytes: int = 1048576) -> Any:
    """Reject duplicate keys, YAML aliases and excessive nesting before construction."""
    if len(text.encode()) > max_bytes:
        raise ValueError("Document too large")
    depth = 0
    for event in yaml.parse(text):
        if isinstance(event, yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > 32:
                raise ValueError("Document too deeply nested")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                if key in result:
                    raise ValueError("Duplicate document key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    data = yaml.load(text, Loader=Loader)
    check_json(data)
    return data
