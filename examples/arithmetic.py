"""Run with: python -m examples.arithmetic"""

import ray

from maze import DAG, Input, Node, Output, Runtime


def double(x: int) -> dict[str, int]:
    return {"value": 2 * x}


def offset(x: int) -> dict[str, int]:
    return {"value": x + 1}


def merge(a: int, b: int) -> dict[str, int]:
    return {"total": a + b}


dag = DAG(
    inputs=("x",),
    nodes=(
        Node("double", double, {"x": Input("x")}),
        Node("offset", offset, {"x": Input("x")}),
        Node(
            "merge",
            merge,
            {"a": Output("double"), "b": Output("offset")},
            outputs=("total",),
        ),
    ),
    outputs={"result": Output("merge", "total")},
)


if __name__ == "__main__":
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
    )
    try:
        runtime = Runtime(dag)
        print(runtime.invoke({"x": 3}))  # {'result': 10}
        print(runtime.invoke({"x": 7}))  # {'result': 22}
    finally:
        ray.shutdown()
