import pytest

from maze.client.maze import client as client_module
from maze.client.maze.client import MaClient


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return dict(self.payload)


@pytest.mark.parametrize(
    ("requested_backend", "normalized_backend"),
    [
        (" VLLM ", "vllm"),
        (" TrAnSfOrMeRs ", "transformers"),
    ],
)
def test_client_normalizes_backend_before_request(
    monkeypatch,
    requested_backend,
    normalized_backend,
):
    requests = []

    def post(url, json):
        requests.append((url, dict(json)))
        return _Response({
            "instance_id": "instance-1",
            "host": "10.0.0.2",
            "port": "8000",
            "endpoint": "http://10.0.0.2:8000/v1",
            "backend": normalized_backend,
        })

    monkeypatch.setattr(client_module.requests, "post", post)
    client = MaClient("http://maze:8000")

    instance_id = client.start_llm_instance(
        "/models/qwen",
        backend=requested_backend,
    )

    assert instance_id == "instance-1"
    assert requests[0][1]["backend"] == normalized_backend
    assert client.llm_instance[instance_id]["backend"] == normalized_backend


@pytest.mark.parametrize("backend", ["", "other", 3])
def test_client_rejects_invalid_backend_before_request(monkeypatch, backend):
    monkeypatch.setattr(
        client_module.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid backend must fail before the HTTP request"
        ),
    )

    with pytest.raises(ValueError):
        MaClient().start_llm_instance("/models/qwen", backend=backend)


def test_client_retains_instance_handle_if_core_returns_wrong_backend(monkeypatch):
    monkeypatch.setattr(
        client_module.requests,
        "post",
        lambda *_args, **_kwargs: _Response({
            "instance_id": "instance-1",
            "host": "10.0.0.2",
            "port": "8000",
            "backend": "transformers",
        }),
    )
    client = MaClient()

    with pytest.raises(Exception, match="instance instance-1"):
        client.start_llm_instance("/models/qwen", backend="vllm")

    assert client.llm_instance["instance-1"] == {
        "instance_id": "instance-1",
        "host": "10.0.0.2",
        "port": "8000",
        "backend": "transformers",
    }
