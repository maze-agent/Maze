from dataclasses import replace

import pytest

from examples.arithmetic import dag, double
from maze import DAG, Input, Literal, Node, Output, compile


def test_compile_orders_dependencies_and_snapshots_bindings() -> None:
    bindings = {"x": Input("x")}
    graph = replace(
        dag,
        nodes=(dag.nodes[2], dag.nodes[1], replace(dag.nodes[0], inputs=bindings)),
    )
    plan = compile(graph)
    bindings["x"] = Literal(99)
    assert plan.steps[-1].id == "merge"
    assert next(s for s in plan.steps if s.id == "double").inputs == (
        ("x", Input("x")),
    )


@pytest.mark.parametrize(
    ("graph", "message"),
    [
        (replace(dag, nodes=dag.nodes + (dag.nodes[0],)), "Duplicate node"),
        (replace(dag, inputs=("x", "x")), "Duplicate graph"),
        (replace(dag, inputs=()), "Unknown graph input"),
        (replace(dag, outputs={"x": Output("merge", "missing")}), "Unknown output"),
        (replace(dag, outputs={"x": Output("double")}), "do not contribute"),
        (
            replace(dag, nodes=(replace(dag.nodes[0], num_cpus=-1), *dag.nodes[1:])),
            "Invalid resources",
        ),
        (
            DAG((), (Node("a", double, {"x": Output("a")}),), {"x": Output("a")}),
            "cycle",
        ),
    ],
)
def test_reject_invalid_graph(graph: DAG, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compile(graph)
