"""Python builds the same versioned model as examples/arithmetic.yaml."""

import asyncio

import ray

from maze.engine import Engine
from maze.spec import (
    DagSpec,
    Edge,
    Endpoint,
    FinalOutput,
    GraphInput,
    InvokeRequest,
    NodeSpec,
    Port,
)
from maze_examples.arithmetic import double, merge, offset  # noqa: F401

spec = DagSpec(
    schema_version="maze.dag/v1",
    name="arithmetic",
    inputs={"x": Port(type="number")},
    nodes=(
        NodeSpec(
            id="double",
            callable="maze_examples.arithmetic:double",
            inputs={"x": Port(type="number")},
            outputs={"value": Port(type="number")},
        ),
        NodeSpec(
            id="offset",
            callable="maze_examples.arithmetic:offset",
            inputs={"x": Port(type="number")},
            outputs={"value": Port(type="number")},
        ),
        NodeSpec(
            id="merge",
            callable="maze_examples.arithmetic:merge",
            inputs={"a": Port(type="number"), "b": Port(type="number")},
            outputs={"total": Port(type="number")},
        ),
    ),
    edges=(
        Edge(source=GraphInput(input="x"), to=Endpoint(node="double", port="x")),
        Edge(source=GraphInput(input="x"), to=Endpoint(node="offset", port="x")),
        Edge(
            source=Endpoint(node="double", port="value"),
            to=Endpoint(node="merge", port="a"),
        ),
        Edge(
            source=Endpoint(node="offset", port="value"),
            to=Endpoint(node="merge", port="b"),
        ),
    ),
    outputs={
        "result": FinalOutput(
            source=Endpoint(node="merge", port="total"), type="number"
        )
    },
)

# Low-level Runtime examples/tests reuse the graph compiled from this model.
dag = spec.to_dag()


async def main() -> None:
    engine = Engine()
    try:
        key = await engine.deploy(spec)
        for x in (3, 7):
            result = await engine.invoke(key, InvokeRequest(input={"x": x}))
            print(result["output"])
    finally:
        await engine.close()


if __name__ == "__main__":
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
    )
    try:
        asyncio.run(main())
    finally:
        ray.shutdown()
