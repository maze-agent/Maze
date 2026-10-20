import io
import json
import threading
import wave
from types import SimpleNamespace

import pytest

from maze.core.scheduler.llm_instance import LlmInstanceManager
from maze.core.scheduler.scheduler import Scheduler
from workflows._inference import chat


@pytest.mark.parametrize("template_kwargs", [None, {"enable_thinking": False}])
def test_chat_passes_explicit_template_options_without_changing_defaults(
    monkeypatch, template_kwargs
):
    requests = []

    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(
            json.dumps({"choices": [{"message": {"content": "10:30"}}]}).encode()
        )

    monkeypatch.setattr("workflows._inference.urlopen", urlopen)
    assert (
        chat(
            [{"role": "user", "content": "When?"}],
            base_url="http://node:8001/v1",
            model="qwen",
            api_key="",
            chat_template_kwargs=template_kwargs,
        )
        == "10:30"
    )
    if template_kwargs is None:
        assert "chat_template_kwargs" not in requests[0]
    else:
        assert requests[0]["chat_template_kwargs"] == template_kwargs


def test_chat_rejects_empty_final_answer(monkeypatch):
    monkeypatch.setattr(
        "workflows._inference.urlopen",
        lambda *a, **k: io.BytesIO(
            json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
        ),
    )
    with pytest.raises(ValueError, match="content is empty"):
        chat([], base_url="http://node:8001/v1", model="qwen", api_key="")


def test_whisper_readiness_handles_transformers_text_only_listing(
    tmp_path, monkeypatch
):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "whisper"}))
    monkeypatch.setattr(
        "maze.core.scheduler.llm_instance.requests.get",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"data": []}
        ),
    )
    manager = LlmInstanceManager()
    assert manager._ready_model_id("http://node:8001", str(tmp_path), {}) == str(
        tmp_path
    )
    with pytest.raises(RuntimeError, match="expected"):
        manager._ready_model_id("http://node:8001", "missing-text-model", {})


@pytest.mark.parametrize("text", ["", "ready"])
def test_whisper_warmup_uses_valid_audio_and_accepts_silence(
    tmp_path, monkeypatch, text
):
    model = tmp_path / "arbitrary-folder"
    model.mkdir()
    (model / "config.json").write_text(json.dumps({"model_type": "whisper"}))
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"text": text}
        )

    monkeypatch.setattr("maze.core.scheduler.llm_instance.requests.post", post)
    LlmInstanceManager()._warmup("http://node:8001", str(model))
    url, kwargs = calls[0]
    assert url.endswith("/v1/audio/transcriptions")
    assert kwargs["data"]["model"] == str(model)
    with wave.open(io.BytesIO(kwargs["files"]["file"][1])) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
            1,
            2,
            16000,
        )
        assert audio.getnframes() == 16000


def test_whisper_warmup_rejects_invalid_transcription(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "whisper"}))
    monkeypatch.setattr(
        "maze.core.scheduler.llm_instance.requests.post",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"text": None}
        ),
    )
    with pytest.raises(RuntimeError, match="invalid transcription"):
        LlmInstanceManager()._warmup("http://node:8001", str(tmp_path))


def test_text_model_warmup_still_checks_final_chat_content(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen3"}))
    calls = []
    monkeypatch.setattr(
        "maze.core.scheduler.llm_instance.requests.post",
        lambda url, **kwargs: (
            calls.append((url, kwargs))
            or SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": "READY"}}]},
            )
        ),
    )
    LlmInstanceManager()._warmup("http://node:8001", str(tmp_path))
    assert calls[0][0].endswith("/v1/chat/completions")
    assert calls[0][1]["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def placement_fixture():
    manager = LlmInstanceManager()
    manager.register_instance(
        "whisper",
        "asr",
        "127.0.0.1",
        "node",
        0,
        8001,
        {"gpu": 1, "gpu_mem": 8192},
        backend="transformers",
    )
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.llm_instance_manager = manager
    scheduler._llm_stop_future_lock = threading.Lock()
    scheduler._llm_runtime_cleanup_futures = {}
    cleanups = []
    scheduler._submit_runtime_llm_cleanup = lambda candidate: cleanups.append(candidate)
    decision = {
        "candidate_nodes": [{"node_id": "node", "reject_reasons": ["insufficient_gpu"]}]
    }
    request = {"model": "qwen", "backend": "transformers"}
    return scheduler, manager, cleanups, decision, request


def test_waiting_model_reclaims_idle_asr_before_default_timeout():
    scheduler, manager, cleanups, decision, request = placement_fixture()
    scheduler._reclaim_idle_llm_for_placement(request, decision)
    assert [c["instance_id"] for c in cleanups] == ["whisper"]
    assert manager.id_to_state["whisper"] == "stopping"
    assert (
        manager.route_model_request(
            "run", {"local_model": "asr", "backend": "transformers"}, task_id="asr"
        )
        is None
    )


@pytest.mark.parametrize(
    "condition",
    [
        "inflight",
        "pending",
        "other_node",
        "missing_model",
        "same_model",
        "cleanup_pending",
    ],
)
def test_waiting_model_preserves_instances_that_cannot_be_reclaimed(condition):
    scheduler, manager, cleanups, decision, request = placement_fixture()
    if condition == "inflight":
        manager.route_model_request(
            "run", {"local_model": "asr", "backend": "transformers"}, task_id="asr"
        )
    elif condition == "pending":
        manager.record_model_demand(
            {"local_model": "asr", "backend": "transformers"},
            workflow_id="run",
            task_id="asr",
        )
    elif condition == "other_node":
        decision["candidate_nodes"][0]["node_id"] = "another-node"
    elif condition == "missing_model":
        decision["candidate_nodes"][0]["reject_reasons"].append("missing_model")
    elif condition == "same_model":
        request["model"] = "asr"
    elif condition == "cleanup_pending":
        scheduler._llm_runtime_cleanup_futures["old"] = (None, {"node_id": "node"})
    scheduler._reclaim_idle_llm_for_placement(request, decision)
    assert cleanups == []
    assert manager.id_to_state["whisper"] == "ready"


def test_failed_cleanup_submission_restores_routing():
    scheduler, manager, cleanups, decision, request = placement_fixture()

    def fail(_candidate):
        raise RuntimeError("executor unavailable")

    scheduler._submit_runtime_llm_cleanup = fail
    scheduler._reclaim_idle_llm_for_placement(request, decision)
    assert manager.id_to_state["whisper"] == "ready"
    assert manager.route_model_request(
        "run", {"local_model": "asr", "backend": "transformers"}, task_id="asr"
    )


def test_transcription_rejects_text_model_before_sending_audio(tmp_path, monkeypatch):
    from workflows._inference import transcribe

    (tmp_path / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]})
    )
    monkeypatch.setattr(
        "workflows._inference.requests.post",
        lambda *a, **k: pytest.fail("sent audio to a text model"),
    )
    with pytest.raises(ValueError, match="audio model.*text model"):
        transcribe(
            b"audio",
            file_name="input.wav",
            base_url="http://node:8001/v1",
            model=str(tmp_path),
            api_key="",
        )


@pytest.mark.parametrize("local", [True, False])
def test_transcription_keeps_local_and_remote_audio_models_working(
    tmp_path, monkeypatch, local
):
    import requests
    from workflows._inference import transcribe

    if local:
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "model_type": "whisper",
                    "architectures": ["WhisperForConditionalGeneration"],
                }
            )
        )
    model = str(tmp_path) if local else "remote-speech-model"
    calls = []
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"text":"The meeting starts at 10:30 am."}'
    monkeypatch.setattr(
        "workflows._inference.requests.post",
        lambda url, **kwargs: (calls.append((url, kwargs)) or response),
    )
    result = transcribe(
        b"audio",
        file_name="input.wav",
        base_url="http://node:8001/v1",
        model=model,
        api_key="",
    )
    assert "10:30" in result
    assert calls[0][0] == "http://node:8001/v1/audio/transcriptions"
    assert calls[0][1]["data"]["model"] == model


def test_transcription_http_error_identifies_the_model_and_server_error(monkeypatch):
    import requests
    from workflows._inference import transcribe

    response = requests.Response()
    response.status_code = 500
    response._content = b'{"detail":"Audio processor unavailable"}'
    monkeypatch.setattr("workflows._inference.requests.post", lambda *a, **k: response)
    with pytest.raises(
        requests.HTTPError,
        match="remote-speech-model.*500.*Audio processor unavailable",
    ) as error:
        transcribe(
            b"audio",
            file_name="input.wav",
            base_url="http://node:8001/v1",
            model="remote-speech-model",
            api_key="",
        )
    assert error.value.response is response
