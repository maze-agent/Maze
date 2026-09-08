import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import ray

from examples.arithmetic import dag, double
from maze import DAG, Input, Literal, Node, Output, Runtime


@pytest.fixture(scope="module", autouse=True)
def cluster() -> Iterator[None]:
    assert not ray.is_initialized()
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
    )
    yield
    ray.shutdown()


def rendezvous(directory: str, name: str, other: str) -> dict[str, int]:
    """Both tasks must start before either can finish; no timing speedup claim."""
    root = Path(directory)
    (root / name).touch()
    deadline = time.monotonic() + 15
    while not (root / other).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Sibling task never started")
        time.sleep(0.01)
    return {"pid": os.getpid()}


def fail() -> dict[str, int]:
    raise ValueError("intentional upstream failure")


def write_marker(x: int, path: str) -> dict[str, int]:
    Path(path).touch()
    return {"value": x}


def split(x: int) -> dict[str, int]:
    # Deliberately differs from declared port order.
    return {"right": x + 1, "left": x * 10}


def test_repeated_and_concurrent_invocations() -> None:
    runtime = Runtime(dag)
    assert runtime.invoke({"x": 3}) == {"result": 10}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda x: runtime.invoke({"x": x}), range(8)))
    assert results == [{"result": 3 * x + 1} for x in range(8)]


def test_tasks_run_in_parallel_worker_processes(tmp_path: Path) -> None:
    graph = DAG(
        inputs=(),
        nodes=tuple(
            Node(
                name,
                rendezvous,
                {
                    "directory": Literal(str(tmp_path)),
                    "name": Literal(name),
                    "other": Literal(other),
                },
                outputs=("pid",),
            )
            for name, other in (("a", "b"), ("b", "a"))
        ),
        outputs={"a": Output("a", "pid"), "b": Output("b", "pid")},
    )
    result = Runtime(graph).invoke({})
    assert result["a"] != result["b"]
    assert os.getpid() not in result.values()


def test_multiple_ports_and_top_level_dependencies() -> None:
    graph = DAG(
        inputs=("x",),
        nodes=(
            Node("split", split, {"x": Input("x")}, outputs=("left", "right")),
            Node("double", double, {"x": Output("split", "left")}),
        ),
        outputs={"left": Output("double"), "right": Output("split", "right")},
    )
    assert Runtime(graph).invoke({"x": 3}) == {"left": 60, "right": 4}


def test_failure_blocks_downstream_and_fails_whole_graph(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    graph = DAG(
        inputs=(),
        nodes=(
            Node("ok", double, {"x": Literal(3)}),
            Node("fail", fail, {}),
            Node(
                "after",
                write_marker,
                {"x": Output("fail"), "path": Literal(str(marker))},
            ),
        ),
        outputs={"ok": Output("ok"), "bad": Output("after")},
    )
    with pytest.raises(
        ray.exceptions.RayTaskError, match="intentional upstream failure"
    ):
        Runtime(graph).invoke({})
    assert not marker.exists()


def test_invocation_inputs_are_checked() -> None:
    with pytest.raises(ValueError, match="Expected graph inputs"):
        Runtime(dag).invoke({"wrong": 3})


class WorkerOnly:
    def __init__(self, driver_pid: int, payload: bytes) -> None:
        self.driver_pid = driver_pid
        self.payload = payload

    def __reduce__(self) -> tuple:
        return restore_worker_only, (self.driver_pid, self.payload)


def restore_worker_only(driver_pid: int, payload: bytes) -> WorkerOnly:
    if os.getpid() == driver_pid:
        raise AssertionError("Intermediate payload returned to the coordinator")
    return WorkerOnly(driver_pid, payload)


def produce(driver_pid: int) -> dict[str, WorkerOnly]:
    return {"value": WorkerOnly(driver_pid, b"x" * (8 * 1024**2))}


def consume(value: WorkerOnly) -> dict[str, int]:
    return {"value": len(value.payload)}


def test_large_intermediate_never_deserializes_in_driver() -> None:
    graph = DAG(
        (),
        (
            Node("produce", produce, {"driver_pid": Literal(os.getpid())}),
            Node("consume", consume, {"value": Output("produce")}),
        ),
        {"size": Output("consume")},
    )
    assert Runtime(graph).invoke({}) == {"size": 8 * 1024**2}
