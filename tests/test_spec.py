import json
from pathlib import Path

import pytest

from maze.spec import DagSpec, parse_document


def arithmetic_spec() -> DagSpec:
    return DagSpec.model_validate(
        parse_document(
            (Path(__file__).parents[1] / "examples/arithmetic.yaml").read_text()
        )
    )


def test_json_yaml_and_python_use_same_graph() -> None:
    spec = arithmetic_spec()
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
