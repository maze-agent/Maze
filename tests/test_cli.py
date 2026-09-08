import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from examples.arithmetic import spec as python_spec
from maze.client import MazeClient
from maze.spec import InvokeRequest
from tests.test_engine import delay_spec


def send_invocation(port: int, key: str, invocation_id: str) -> socket.socket:
    data = json.dumps(
        {"input": {"x": 1, "seconds": 20}, "invocation_id": invocation_id}
    ).encode()
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.sendall(
        (
            f"POST /v1/deployments/{key}/invoke HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer temporary-test-token\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n\r\n"
        ).encode()
        + data
    )
    return connection


def wait_counts(client: httpx.Client, key: str, active: int, waiting: int) -> None:
    deadline = time.monotonic() + 10
    while True:
        status = client.get(f"/v1/deployments/{key}").json()
        if (status["active_invocations"], status["waiting_invocations"]) == (
            active,
            waiting,
        ):
            return
        assert time.monotonic() < deadline, status
        time.sleep(0.02)


@pytest.mark.parametrize("stop_via_signal", [False, True])
def test_managed_local_cli_starts_and_stops_owned_process(
    tmp_path: Path, stop_via_signal: bool
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "engine.json"
    config.write_text(
        json.dumps(
            {
                "api": {"port": port},
                "limits": {"max_active_invocations": 1},
                "timeouts": {"shutdown_grace_s": 0.2},
            }
        )
    )
    env = {**os.environ, "MAZE_API_TOKEN": "temporary-test-token"}
    log = tmp_path / "server.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "maze", "start", "--config", str(config)],
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=20,
                headers={"Authorization": "Bearer temporary-test-token"},
            ) as client:
                deadline = time.monotonic() + 25
                while True:
                    assert process.poll() is None, log.read_text()
                    try:
                        if client.get("/readyz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.1)
                with MazeClient(
                    f"http://127.0.0.1:{port}", "temporary-test-token"
                ) as sdk:
                    key = sdk.deploy(python_spec)["deployment_id"]
                    result = sdk.invoke(key, InvokeRequest(input={"x": 3}))
                    assert result["output"] == {"result": 10}
                    assert sdk.undeploy(key) == {"status": "removed"}
                delayed = client.post(
                    "/v1/deployments",
                    json={
                        "dag": delay_spec().model_dump(
                            by_alias=True, exclude_unset=True
                        )
                    },
                )
                assert delayed.status_code == 200, delayed.text
                delay_id = delayed.json()["deployment_id"]
                with send_invocation(port, delay_id, "network_active"):
                    wait_counts(client, delay_id, active=1, waiting=0)
                    with send_invocation(port, delay_id, "network_queued"):
                        wait_counts(client, delay_id, active=1, waiting=1)
                    wait_counts(client, delay_id, active=1, waiting=0)
                wait_counts(client, delay_id, active=0, waiting=0)
                for invocation_id in ("network_active", "network_queued"):
                    assert (
                        client.post(
                            f"/v1/invocations/{invocation_id}/cancel"
                        ).status_code
                        == 404
                    )
                result = client.post(
                    f"/v1/deployments/{delay_id}/invoke",
                    json={"input": {"x": 9, "seconds": 0}},
                )
                assert result.status_code == 200 and result.json()["output"] == {
                    "result": 9
                }
                with send_invocation(port, delay_id, "stop_active"):
                    wait_counts(client, delay_id, active=1, waiting=0)
                    if stop_via_signal:
                        process.terminate()
                    else:
                        assert client.post("/v1/stop").status_code == 200
                    code = process.wait(timeout=10)
            # Ray's signal handler may translate SIGTERM to SystemExit(15).
            expected = (0, signal.SIGTERM, -signal.SIGTERM) if stop_via_signal else (0,)
            assert code in expected, log.read_text()
            processes = subprocess.check_output(["ps", "-axo", "comm="], text=True)
            assert not any(
                Path(p.strip()).name in ("raylet", "gcs_server")
                for p in processes.splitlines()
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
