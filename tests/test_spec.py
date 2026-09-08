import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from maze.spec import DagSpec, InvokeRequest, Port, parse_document


def arithmetic_spec() -> DagSpec:
    return DagSpec.model_validate(
        parse_document(
            (Path(__file__).parents[1] / "examples/arithmetic.yaml").read_text()
        )
    )


def test_json_yaml_and_python_use_same_graph() -> None:
    from examples.arithmetic import spec as python_spec

    spec = arithmetic_spec()
    assert python_spec == spec
    assert spec.to_dag().inputs == ("x",)
    data = parse_document(spec.model_dump_json(by_alias=True, exclude_unset=True))
    assert DagSpec.model_validate(data) == spec
    assert "maze.dag/v1" in json.dumps(DagSpec.model_json_schema())


@pytest.mark.parametrize(
    "text",
    [
        "x: 1\nx: 2",
        "x: &a [1]\ny: *a",
        "[" * 33 + "0" + "]" * 33,
        "x: !!python/object:builtins.object {}",
        "x: .nan",
        "x: {1: value}",
        "x: {null: value}",
        "x: !!set {a: null}",
    ],
)
def test_reject_unsafe_or_ambiguous_documents(text: str) -> None:
    with pytest.raises(Exception):
        parse_document(text)


def test_unknown_version_and_fields() -> None:
    data = arithmetic_spec().model_dump(by_alias=True, exclude_unset=True)
    for field, value in (("schema", "maze.dag/v999"), ("unexpected", 1)):
        with pytest.raises(ValueError):
            DagSpec.model_validate({**data, field: value})


def test_duplicate_edge_and_missing_binding() -> None:
    spec = arithmetic_spec()
    with pytest.raises(ValueError, match="Multiple bindings"):
        spec.model_copy(update={"edges": spec.edges + (spec.edges[0],)}).to_dag()
    with pytest.raises(ValueError, match="Missing input"):
        spec.model_copy(update={"edges": spec.edges[1:]}).to_dag()


def test_port_type_does_not_accept_bool_as_number() -> None:
    with pytest.raises(ValueError):
        arithmetic_spec().check_inputs({"x": True})


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["inputs"]["x"].update(default="bad"),
        lambda d: d["nodes"][0]["inputs"]["x"].update(default="bad"),
        lambda d: d["outputs"]["result"].update(type="string"),
        lambda d: d["outputs"]["result"]["from"].update(port="missing"),
        lambda d: d["nodes"].append(d["nodes"][0]),
        lambda d: d["nodes"][0].update(callable="maze_examples.arithmetic:missing"),
        lambda d: d["nodes"][0].update(callable="maze_examples.arithmetic:asyncio"),
        lambda d: d["nodes"][0].update(execution="async_actor"),
        lambda d: d["nodes"][0].update(constants={"x": {"nested": float("inf")}}),
    ],
)
def test_invalid_contracts_fail_before_execution(
    change: Callable[[dict], None],
) -> None:
    data = arithmetic_spec().document()
    change(data)
    with pytest.raises((ValueError, AttributeError)):
        DagSpec.model_validate(data).to_dag()


def test_defaults_survive_wire_roundtrip_and_do_not_leak_between_calls() -> None:
    data = arithmetic_spec().document()
    data["inputs"]["x"]["default"] = 3
    spec = DagSpec.model_validate(data)
    restored = DagSpec.model_validate(
        parse_document(spec.model_dump_json(by_alias=True, exclude_unset=True))
    )
    assert restored.check_inputs({}) == {"x": 3}
    assert "default" not in Port(type="any").document()
    assert Port(type="any", default=None).document()["default"] is None
    spec = spec.model_copy(update={"inputs": {"items": Port(type="array", default=[])}})
    first = spec.check_inputs({})
    first["items"].append("changed")
    assert spec.check_inputs({}) == {"items": []}


def test_serialization_preserves_mutated_containers_and_absent_defaults() -> None:
    spec = arithmetic_spec()
    spec.nodes[0].constants["x"] = 4
    restored = DagSpec.model_validate(spec.document())
    assert restored.nodes[0].constants == {"x": 4}
    assert "default" not in restored.inputs["x"].model_fields_set
    with pytest.raises(ValueError, match="constant binding"):
        restored.to_dag()


@pytest.mark.parametrize("value", [{1: "value"}, (1, 2), {"x": [float("nan")]}])
def test_python_defaults_reject_non_json_values(value: Any) -> None:
    with pytest.raises(ValueError):
        Port(default=value)
    with pytest.raises(ValueError):
        InvokeRequest(input={"x": value})
