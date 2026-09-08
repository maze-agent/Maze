import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.test_spec import arithmetic_spec


def test_managed_local_cli_starts_and_stops_owned_process(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "engine.json"
    config.write_text(json.dumps({"api": {"port": port}}))
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
                deployed = client.post(
                    "/v1/deployments",
                    json={
                        "dag": arithmetic_spec().model_dump(
                            by_alias=True, exclude_unset=True
                        )
                    },
                )
                assert deployed.status_code == 200, deployed.text
                key = deployed.json()["deployment_id"]
                result = client.post(
                    f"/v1/deployments/{key}/invoke", json={"input": {"x": 3}}
                )
                assert result.status_code == 200, result.text
                assert result.json()["output"] == {"result": 10}
                assert client.post("/v1/stop").status_code == 200
            assert process.wait(timeout=15) == 0, log.read_text()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
