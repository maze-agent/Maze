from __future__ import annotations

from maze.core.scheduler.resource import Node, ResourceManager


CPU_TASK = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def node_resources(cpu: int = 4):
    return {
        "cpu": cpu,
        "cpu_mem": 1024,
        "gpu_resource": {},
    }


def make_manager():
    manager = ResourceManager()
    manager._refresh_head_local_models = lambda: None
    manager._ray_node_index = lambda: {}
    manager.head_node_id = "node-a"
    for node_id, node_ip in (("node-a", "10.0.0.1"), ("node-b", "10.0.0.2")):
        resources = node_resources()
        manager.nodes[node_id] = Node(node_id, node_ip, resources, resources)
        manager.running_task_counts[node_id] = 0
    return manager


def test_dag_context_prefers_existing_workflow_node():
    manager = make_manager()

    first = manager.select_node(CPU_TASK, workflow_id="run-1")
    second = manager.select_node(CPU_TASK, workflow_id="run-1")

    assert first.node_id == "node-a"
    assert second.node_id == "node-a"
    assert second.decision["dag_context"]["preferred_node_id"] == "node-a"
    assert second.decision["dag_context"]["affinity_hit"] is True


def test_new_dag_context_uses_least_loaded_context_node():
    manager = make_manager()
    manager.dag_context_manager.record_selection("old-run", "node-a", "10.0.0.1")

    selection = manager.select_node(CPU_TASK, workflow_id="new-run")

    assert selection.node_id == "node-b"
    assert selection.decision["dag_context"]["context_created"] is True
    assert selection.decision["dag_context"]["preferred_node_id"] == "node-b"


def test_dag_context_falls_back_when_affinity_node_lacks_resources():
    manager = make_manager()
    manager.dag_context_manager.record_selection("run-1", "node-a", "10.0.0.1")
    manager.nodes["node-a"].available_resources["cpu"] = 0

    selection = manager.select_node(CPU_TASK, workflow_id="run-1")

    assert selection.node_id == "node-b"
    assert selection.decision["dag_context"]["preferred_node_id"] == "node-a"
    assert selection.decision["dag_context"]["selected_node_id"] == "node-b"
    assert selection.decision["dag_context"]["affinity_hit"] is False
    assert manager.dag_context_manager.get_context("run-1").preferred_node_id == "node-a"


def test_dag_context_release_removes_context_load():
    manager = make_manager()
    manager.dag_context_manager.record_selection("run-1", "node-a", "10.0.0.1")

    assert manager.release_dag_context("run-1") is True
    assert manager.dag_context_manager.get_context("run-1") is None
    assert manager.dag_context_manager.node_context_load("node-a") == 0
