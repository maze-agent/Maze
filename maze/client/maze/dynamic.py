import json
import time
from json import JSONDecodeError
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests
import websocket

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.models import TaskOutput, TaskOutputs


def _metadata_param_payload(names: List[str], data_types: Dict[str, str]) -> List[Dict[str, str]]:
    return [
        {
            "name": name,
            "data_type": data_types.get(name, "str"),
        }
        for name in names
    ]


def task_spec_payload_from_func(
    task_func: Callable,
    task_spec_id: str | None = None,
    task_name: str | None = None,
) -> Dict[str, Any]:
    metadata = get_task_metadata(task_func)
    return {
        "task_spec_id": task_spec_id,
        "task_name": task_name or metadata.func_name,
        "code_str": metadata.code_str,
        "code_ser": metadata.code_ser,
        "inputs": _metadata_param_payload(metadata.inputs, metadata.data_types),
        "outputs": _metadata_param_payload(metadata.outputs, metadata.data_types),
        "resources": metadata.resources,
    }


def _encode_dynamic_input(value: Any) -> Any:
    if isinstance(value, TaskOutput):
        return {
            "__maze_output_ref__": True,
            "task_id": value.task_id,
            "output_key": value.output_key,
        }
    return value


def _encode_dynamic_inputs(inputs: Dict[str, Any] | None) -> Dict[str, Any]:
    return {
        key: _encode_dynamic_input(value)
        for key, value in (inputs or {}).items()
    }


class DynamicTaskSpec:
    def __init__(
        self,
        run: "DynamicRun",
        task_spec_id: str,
        task_name: str,
        output_keys: List[str],
    ):
        self.run = run
        self.task_spec_id = task_spec_id
        self.task_name = task_name
        self.output_keys = output_keys

    def __repr__(self) -> str:
        return f"DynamicTaskSpec(id='{self.task_spec_id}', name='{self.task_name}')"


class DynamicTaskInvocation:
    def __init__(
        self,
        run: "DynamicRun",
        task_id: str,
        task_name: str,
        output_keys: List[str],
        idempotent: bool = False,
    ):
        self.run = run
        self.task_id = task_id
        self.task_name = task_name
        self.idempotent = idempotent
        self.outputs = TaskOutputs(task_id, output_keys) if output_keys else None

    def __repr__(self) -> str:
        return f"DynamicTaskInvocation(id='{self.task_id[:8]}...', name='{self.task_name}')"


class DynamicRun:
    def __init__(self, run_id: str, server_url: str):
        self.run_id = run_id
        self.server_url = server_url.rstrip("/")

    def register_task_spec(
        self,
        task_func: Callable,
        task_spec_id: str | None = None,
        task_name: str | None = None,
    ) -> DynamicTaskSpec:
        payload = task_spec_payload_from_func(task_func, task_spec_id, task_name)
        response = requests.post(
            f"{self.server_url}/dynamic_runs/{self.run_id}/task_specs",
            json=payload,
        )
        if response.status_code != 200:
            raise Exception(f"Failed to register dynamic task spec: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to register dynamic task spec: {data.get('message', 'Unknown error')}")

        output_keys = [item["name"] for item in data.get("outputs", [])]
        return DynamicTaskSpec(
            run=self,
            task_spec_id=data["task_spec_id"],
            task_name=data["task_name"],
            output_keys=output_keys,
        )

    def append_task(
        self,
        task: DynamicTaskSpec | Callable | str,
        inputs: Dict[str, Any] | None = None,
        parents: List[DynamicTaskInvocation | str] | None = None,
        request_id: str | None = None,
        task_name: str | None = None,
    ) -> DynamicTaskInvocation:
        payload: Dict[str, Any] = {
            "inputs": _encode_dynamic_inputs(inputs),
            "parents": [
                parent.task_id if isinstance(parent, DynamicTaskInvocation) else parent
                for parent in (parents or [])
            ],
            "request_id": request_id,
        }

        if isinstance(task, DynamicTaskSpec):
            payload["task_spec_id"] = task.task_spec_id
        elif isinstance(task, str):
            payload["task_spec_id"] = task
        elif callable(task):
            payload["task_spec"] = task_spec_payload_from_func(task, task_name=task_name)
        else:
            raise TypeError("append_task expects a DynamicTaskSpec, task_spec_id, or @task function")

        response = requests.post(
            f"{self.server_url}/dynamic_runs/{self.run_id}/append_task",
            json=payload,
        )
        if response.status_code != 200:
            raise Exception(f"Failed to append dynamic task: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to append dynamic task: {data.get('message', 'Unknown error')}")

        output_keys = [item["name"] for item in data.get("outputs", [])]
        return DynamicTaskInvocation(
            run=self,
            task_id=data["task_id"],
            task_name=data["task_name"],
            output_keys=output_keys,
            idempotent=data.get("idempotent", False),
        )

    def finalize(self, result: Any = None):
        response = requests.post(
            f"{self.server_url}/dynamic_runs/{self.run_id}/finalize",
            json={"result": result},
        )
        if response.status_code != 200:
            raise Exception(f"Failed to finalize dynamic run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to finalize dynamic run: {data.get('message', 'Unknown error')}")

        return data

    def cancel(self, reason: str | None = None):
        response = requests.post(
            f"{self.server_url}/dynamic_runs/{self.run_id}/cancel",
            json={"reason": reason},
        )
        if response.status_code != 200:
            raise Exception(f"Failed to cancel dynamic run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to cancel dynamic run: {data.get('message', 'Unknown error')}")

        return data

    def delete(self):
        response = requests.delete(f"{self.server_url}/dynamic_runs/{self.run_id}")
        if response.status_code != 200:
            raise Exception(f"Failed to delete dynamic run: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to delete dynamic run: {data.get('message', 'Unknown error')}")

        return data

    def get_status(self) -> Dict[str, Any]:
        response = requests.get(f"{self.server_url}/dynamic_runs/{self.run_id}")
        if response.status_code != 200:
            raise Exception(f"Failed to get dynamic run status: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get dynamic run status: {data.get('message', 'Unknown error')}")

        return data.get("run", {})

    def status(self) -> Dict[str, Any]:
        return self.get_status()

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        params = {"after": after} if after is not None else None
        response = requests.get(f"{self.server_url}/dynamic_runs/{self.run_id}/events", params=params)
        if response.status_code != 200:
            raise Exception(f"Failed to get dynamic run events: {response.status_code}, {response.text}")

        data = response.json()
        if data.get("status") != "success":
            raise Exception(f"Failed to get dynamic run events: {data.get('message', 'Unknown error')}")

        return data.get("events", [])

    def emit_event(self, event_type: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
        response = requests.post(
            f"{self.server_url}/dynamic_runs/{self.run_id}/events",
            json={
                "type": event_type,
                "data": data or {},
            },
        )
        if response.status_code != 200:
            raise Exception(f"Failed to emit dynamic run event: {response.status_code}, {response.text}")

        payload = response.json()
        if payload.get("status") != "success":
            raise Exception(f"Failed to emit dynamic run event: {payload.get('message', 'Unknown error')}")

        return payload.get("event", {})

    def wait_for_task(
        self,
        task: DynamicTaskInvocation | str,
        timeout: float | None = None,
        poll_interval: float = 0.2,
    ) -> Dict[str, Any]:
        task_id = task.task_id if isinstance(task, DynamicTaskInvocation) else task
        deadline = None if timeout is None else time.time() + timeout
        after = None

        while True:
            events = self.get_events(after=after)
            for event in events:
                after = max(after or 0, int(event.get("seq", 0)))
                event_type = event.get("type")
                data = event.get("data", {})
                if data.get("task_id") == task_id and event_type == "finish_task":
                    return event
                if data.get("task_id") == task_id and event_type == "task_exception":
                    raise RuntimeError(f"Dynamic task failed: {data.get('result', 'Unknown error')}")
                if event_type in ("finish_workflow", "cancel_dynamic_run", "timeout_dynamic_run", "interrupt_dynamic_run"):
                    raise RuntimeError(f"Dynamic run ended before task finished: {event_type}")

            if deadline is not None and time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for dynamic task: {task_id}")

            time.sleep(poll_interval)

    def stream_events(self) -> Iterator[Dict[str, Any]]:
        ws_url = self.server_url.replace("http://", "ws://").replace("https://", "wss://")
        ws = websocket.create_connection(f"{ws_url}/dynamic_runs/{self.run_id}/events")
        try:
            while True:
                message = ws.recv()
                if not message:
                    return
                yield json.loads(message)
        except (websocket.WebSocketConnectionClosedException, JSONDecodeError):
            return
        finally:
            ws.close()

    def __repr__(self) -> str:
        return f"DynamicRun(id='{self.run_id}')"
