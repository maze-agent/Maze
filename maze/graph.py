"""The graph is data. Compilation validates it and fixes submission order."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from inspect import iscoroutinefunction, signature
from math import isfinite
from typing import Any, TypeAlias
from typing import Literal as Choice

# Values between arbitrary user functions may be any Ray-serializable object.
Values: TypeAlias = Mapping[str, Any]
Function: TypeAlias = Callable[..., Values | Awaitable[Values]]


@dataclass(frozen=True)
class Input:
    name: str


@dataclass(frozen=True)
class Output:
    node: str
    port: str = "value"


@dataclass(frozen=True)
class Literal:
    value: Any


Source: TypeAlias = Input | Output | Literal


@dataclass(frozen=True)
class Node:
    id: str
    function: Function
    inputs: Mapping[str, Source]
    outputs: tuple[str, ...] = ("value",)
    num_cpus: float = 1
    num_gpus: float = 0
    execution: Choice["task", "async_actor"] = "task"
    max_concurrency: int = 8
    memory_bytes: int | None = None


@dataclass(frozen=True)
class DAG:
    inputs: tuple[str, ...]
    nodes: tuple[Node, ...]
    outputs: Mapping[str, Output]


@dataclass(frozen=True)
class Step:
    id: str
    function: Function
    inputs: tuple[tuple[str, Source], ...]
    outputs: tuple[str, ...]
    num_cpus: float
    num_gpus: float
    execution: Choice["task", "async_actor"]
    max_concurrency: int
    memory_bytes: int | None


@dataclass(frozen=True)
class Plan:
    inputs: tuple[str, ...]
    steps: tuple[Step, ...]
    outputs: tuple[tuple[str, Output], ...]


def compile(dag: DAG) -> Plan:
    """Snapshot graph structure; leave scheduling and dependency waits to Ray.

    User functions and literal values must remain unchanged after compilation.
    """
    nodes = {node.id: node for node in dag.nodes}
    if len(nodes) != len(dag.nodes):
        raise ValueError("Duplicate node IDs")
    if len(set(dag.inputs)) != len(dag.inputs):
        raise ValueError("Duplicate graph inputs")
    if not dag.outputs:
        raise ValueError("A DAG needs at least one final output")

    def check_output(ref: Output) -> None:
        if ref.node not in nodes or ref.port not in nodes[ref.node].outputs:
            raise ValueError(f"Unknown output: {ref}")

    dependencies: dict[str, set[str]] = {}
    for node in dag.nodes:
        if not node.outputs or len(set(node.outputs)) != len(node.outputs):
            raise ValueError(f"Invalid output ports: {node.id}")
        if any(not isfinite(n) or n < 0 for n in (node.num_cpus, node.num_gpus)):
            raise ValueError(f"Invalid resources: {node.id}")
        if node.execution not in ("task", "async_actor"):
            raise ValueError(f"Unsupported execution: {node.execution}")
        if not callable(node.function) or (
            iscoroutinefunction(node.function) != (node.execution == "async_actor")
        ):
            raise ValueError(f"Function does not match execution mode: {node.id}")
        if node.max_concurrency < 1 or (
            node.memory_bytes is not None and node.memory_bytes <= 0
        ):
            raise ValueError(f"Invalid concurrency or memory: {node.id}")
        signature(node.function).bind(**dict.fromkeys(node.inputs))
        dependencies[node.id] = set()
        for source in node.inputs.values():
            match source:
                case Input(name):
                    if name not in dag.inputs:
                        raise ValueError(f"Unknown graph input: {name}")
                case Output(parent, _):
                    check_output(source)
                    dependencies[node.id].add(parent)
                case Literal():
                    pass
                case _:
                    raise TypeError(f"Expected Input, Output or Literal: {source!r}")

    for ref in dag.outputs.values():
        check_output(ref)
    try:
        order = tuple(TopologicalSorter(dependencies).static_order())
    except CycleError as exc:
        raise ValueError("The graph contains a cycle") from exc

    # Every task must contribute to a returned output, including side effects.
    required = {ref.node for ref in dag.outputs.values()}
    for node_id in reversed(order):
        if node_id in required:
            required.update(dependencies[node_id])
    if unused := nodes.keys() - required:
        raise ValueError(f"Nodes do not contribute to final outputs: {sorted(unused)}")

    steps = tuple(
        Step(
            node.id,
            node.function,
            tuple(node.inputs.items()),
            tuple(node.outputs),
            node.num_cpus,
            node.num_gpus,
            node.execution,
            node.max_concurrency,
            node.memory_bytes,
        )
        for node_id in order
        for node in (nodes[node_id],)
    )
    return Plan(tuple(dag.inputs), steps, tuple(dag.outputs.items()))
