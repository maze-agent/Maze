import sys
from argparse import Namespace

import pytest

from maze.cli import cli


def serve_args(**overrides):
    values = {
        "model": "/models/qwen",
        "backend": "vllm",
        "server_url": "http://maze:8000",
        "cpu": 5,
        "memory": 1024,
        "gpu_memory": 0,
        "gpu_memory_utilization": 0.8,
        "max_model_len": 4096,
        "timeout": 600,
        "json": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_request_core_accepts_ready_instance_status(monkeypatch):
    response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "json": lambda self: {"status": "ready", "backend": "vllm"},
        },
    )()
    monkeypatch.setattr(cli.requests, "request", lambda *args, **kwargs: response)

    assert cli._request_core("POST", "http://maze:8000", "/start_llm_instance") == {
        "status": "ready",
        "backend": "vllm",
    }


@pytest.mark.parametrize(
    ("args", "backend_payload"),
    [
        (
            serve_args(),
            {
                "gpu_memory_utilization": 0.8,
                "max_model_len": 4096,
            },
        ),
        (
            serve_args(
                backend="transformers",
                gpu_memory_utilization=None,
                max_model_len=None,
            ),
            {},
        ),
    ],
)
def test_model_serve_posts_scheduler_request_and_prints_endpoint(
    monkeypatch,
    capsys,
    args,
    backend_payload,
):
    captured = {}

    def fake_request(method, server_url, path, **kwargs):
        captured.update(
            method=method,
            server_url=server_url,
            path=path,
            kwargs=kwargs,
        )
        return {
            "status": "ready",
            "model": args.model,
            "backend": args.backend,
            "host": "10.0.0.2",
            "port": "8123",
            "instance_id": "instance-1",
            "endpoint": "http://10.0.0.2:8123/v1",
        }

    monkeypatch.setattr(cli, "_request_core", fake_request)
    cli._model_serve(args)

    assert captured == {
        "method": "POST",
        "server_url": "http://maze:8000",
        "path": "/start_llm_instance",
        "kwargs": {
            "json": {
                "model": "/models/qwen",
                "backend": args.backend,
                "cpu_nums": 5,
                "memory_mib": 1024,
                "gpu_nums": 1,
                "gpu_mem": 0,
                **backend_payload,
            },
            "timeout": 600,
        },
    }
    output = capsys.readouterr().out
    assert "Instance: instance-1" in output
    assert f"Backend: {args.backend}" in output
    assert "Endpoint: http://10.0.0.2:8123/v1" in output


@pytest.mark.parametrize(
    ("extra_args", "expected_backend"),
    [
        (["--gpu-memory-utilization", "0.75", "--max-model-len", "2048"], "vllm"),
        (["--backend", "transformers"], "transformers"),
    ],
)
def test_model_parser_dispatches_serve(monkeypatch, extra_args, expected_backend):
    captured = {}
    monkeypatch.setattr(cli, "_model_serve", lambda args: captured.update(vars(args)))
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "maze",
            "model",
            "serve",
            "/models/qwen",
            *extra_args,
        ],
    )

    cli.main()

    assert captured["model_command"] == "serve"
    assert captured["model"] == "/models/qwen"
    assert captured["backend"] == expected_backend
    if expected_backend == "vllm":
        assert captured["gpu_memory_utilization"] == 0.75
        assert captured["max_model_len"] == 2048


def test_model_serve_rejects_backend_fallback(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_request_core",
        lambda *args, **kwargs: {
            "status": "ready",
            "model": "/models/qwen",
            "backend": "vllm",
            "host": "10.0.0.2",
            "port": "8123",
            "instance_id": "instance-1",
            "endpoint": "http://10.0.0.2:8123/v1",
        },
    )

    with pytest.raises(SystemExit, match="started backend 'vllm', expected 'transformers'"):
        cli._model_serve(
            serve_args(
                backend="transformers",
                gpu_memory_utilization=None,
                max_model_len=None,
            )
        )


def test_model_stop_waits_for_scheduler_ack(monkeypatch, capsys):
    captured = {}

    def fake_request(method, server_url, path, **kwargs):
        captured.update(
            method=method,
            server_url=server_url,
            path=path,
            kwargs=kwargs,
        )
        return {"status": "success"}

    monkeypatch.setattr(cli, "_request_core", fake_request)
    cli._model_stop(
        Namespace(
            instance_id="instance-1",
            server_url="http://maze:8000",
            timeout=60,
            json=False,
        )
    )

    assert captured == {
        "method": "POST",
        "server_url": "http://maze:8000",
        "path": "/stop_llm_instance",
        "kwargs": {
            "json": {"instance_id": "instance-1"},
            "timeout": 60,
        },
    }
    assert capsys.readouterr().out == "Stopped instance: instance-1\n"
