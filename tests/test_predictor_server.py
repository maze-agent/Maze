from __future__ import annotations

from fastapi.testclient import TestClient

from maze.core.predictor import server


class FakePredictor:
    def predict(self, task_name, features):
        assert task_name == "llm_process"
        assert features == {"token_count": 7}
        return 42.5


def test_predict_endpoint_returns_actual_predictor_value(monkeypatch):
    monkeypatch.setattr(server, "predictor", FakePredictor())
    client = TestClient(server.app)

    response = client.post(
        "/predict",
        json={"task_name": "llm_process", "features": {"token_count": 7}},
    )

    assert response.status_code == 200
    assert response.json()["predict_time"] == 42.5
    assert response.json()["prediction_source"] == "malearn"


def test_predict_endpoint_rejects_non_object_features(monkeypatch):
    monkeypatch.setattr(server, "predictor", FakePredictor())
    client = TestClient(server.app)

    response = client.post(
        "/predict",
        json={"task_name": "llm_process", "features": ["bad"]},
    )

    assert response.status_code == 400
