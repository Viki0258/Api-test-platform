from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_ai_assistant
from app.services.ai_assistant import AiAssistantError, AiAssistantService
from tests.test_ai_assistant import ai_document, generated_candidate_run


client = TestClient(app)


def candidate_payload(*, count: int = 1) -> dict:
    run = generated_candidate_run(count=count).model_dump(mode="json")
    return {
        "document": ai_document(),
        "candidate_run": run,
        "insights": [
            {
                "case_id": "candidate_1",
                "category": "boundary",
                "rationale": "Exercises the declared lower bound.",
            }
        ],
    }


def draft_payload(*, count: int = 1) -> dict:
    return {
        "document": ai_document(),
        "draft": generated_candidate_run(count=count).model_dump(mode="json"),
    }


@pytest.fixture(autouse=True)
def clear_ai_dependency_override():
    app.dependency_overrides.pop(get_ai_assistant, None)
    yield
    app.dependency_overrides.pop(get_ai_assistant, None)


def test_candidate_evaluation_endpoint_returns_feedback() -> None:
    response = client.post("/api/v1/ai/cases/evaluate", json=candidate_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "candidate_evaluator"
    assert body["provider"] == "mock"
    assert body["evaluated_case_count"] == 1
    assert body["requires_human_review"] is True
    assert 0 <= body["score"] <= 100
    assert isinstance(body["summary"], str) and body["summary"]


def test_draft_evaluation_endpoint_returns_feedback() -> None:
    response = client.post("/api/v1/ai/drafts/evaluate", json=draft_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "draft_evaluator"
    assert body["provider"] == "mock"
    assert body["evaluated_case_count"] == 1
    assert body["requires_human_review"] is True


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/v1/ai/cases/evaluate",
            {**candidate_payload(), "candidate_run": generated_candidate_run(count=11).model_dump(mode="json"), "insights": []},
        ),
        (
            "/api/v1/ai/cases/evaluate",
            {**candidate_payload(), "insights": [{"case_id": "unknown", "category": "boundary", "rationale": "invalid case"}]},
        ),
        (
            "/api/v1/ai/drafts/evaluate",
            {**draft_payload(), "document": {"openapi": "2.0", "paths": {}}},
        ),
    ],
)
def test_evaluation_rejects_invalid_input_without_echoing_source(
    path: str, payload: dict
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_AI_EVALUATION_INPUT"
    assert "synthetic-secret-never-send" not in response.text


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/v1/ai/cases/evaluate",
            {
                "candidate_run": candidate_payload()["candidate_run"],
                "sentinel": "malformed-candidate-request-sentinel",
            },
        ),
        (
            "/api/v1/ai/drafts/evaluate",
            {
                "draft": draft_payload()["draft"],
                "sentinel": "malformed-draft-request-sentinel",
            },
        ),
    ],
)
def test_pydantic_body_validation_returns_stable_sanitized_error(
    path: str, payload: dict
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_AI_EVALUATION_INPUT"
    assert "malformed-" not in response.text


def test_evaluation_returns_provider_not_configured() -> None:
    service = AiAssistantService(Settings(ai_provider="openai"))
    app.dependency_overrides[get_ai_assistant] = lambda: service

    response = client.post("/api/v1/ai/drafts/evaluate", json=draft_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


def test_candidate_evaluation_rejects_oversized_source_without_echoing_it() -> None:
    payload = candidate_payload()
    payload["document"]["x-synthetic-padding"] = (
        "source-too-large-sentinel" + "x" * 1_048_577
    )

    response = client.post("/api/v1/ai/cases/evaluate", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "AI_EVALUATION_SOURCE_TOO_LARGE"
    assert "source-too-large-sentinel" not in response.text


class _UnavailableProvider:
    name = "synthetic-provider"
    model = "synthetic-model"

    def evaluate(self, **_kwargs):
        raise AiAssistantError(
            "AI_PROVIDER_UNAVAILABLE",
            "provider unavailable",
            status_code=502,
        )


class _InvalidOutputProvider:
    name = "synthetic-provider"
    model = "synthetic-model"

    def evaluate(self, **_kwargs):
        return {}


@pytest.mark.parametrize(
    ("provider", "code"),
    [
        (_UnavailableProvider(), "AI_PROVIDER_UNAVAILABLE"),
        (_InvalidOutputProvider(), "AI_PROVIDER_INVALID_OUTPUT"),
    ],
)
def test_evaluation_maps_provider_failures_to_sanitized_502(
    provider, code: str
) -> None:
    service = AiAssistantService(Settings(), provider=provider)
    app.dependency_overrides[get_ai_assistant] = lambda: service

    response = client.post("/api/v1/ai/drafts/evaluate", json=draft_payload())

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == code
    assert "synthetic-secret-never-send" not in response.text
    assert "provider unavailable" not in response.text


def test_evaluation_does_not_construct_executor_or_history_store(monkeypatch) -> None:
    class UnexpectedSideEffect:
        def __init__(self, *args, **kwargs):
            raise AssertionError("evaluation must not execute or persist a run")

    monkeypatch.setattr("app.main.TestExecutor", UnexpectedSideEffect)
    monkeypatch.setattr("app.main.RunHistoryStore", UnexpectedSideEffect)

    response = client.post("/api/v1/ai/drafts/evaluate", json=draft_payload())

    assert response.status_code == 200
    assert response.json()["role"] == "draft_evaluator"


@pytest.mark.parametrize("path", [
    "/api/v1/ai/cases/evaluate",
    "/api/v1/ai/drafts/evaluate",
])
def test_evaluation_endpoints_are_exposed_with_frozen_tags(path: str) -> None:
    operation = app.openapi()["paths"][path]["post"]

    assert operation["tags"] == ["ai-assistant", "ai-evaluation"]
    responses = operation["responses"]
    assert set(("200", "422", "502", "503")).issubset(responses)
    assert responses["200"]["content"]["application/json"]["schema"]

    error_contract = {
        "422": (
            "AI_EVALUATION_SOURCE_TOO_LARGE",
            "INVALID_AI_EVALUATION_INPUT",
        ),
        "502": ("AI_PROVIDER_UNAVAILABLE", "AI_PROVIDER_INVALID_OUTPUT"),
        "503": "AI_PROVIDER_NOT_CONFIGURED",
    }
    for status, codes in error_contract.items():
        serialized = str(responses[status])
        for code in (codes,) if isinstance(codes, str) else codes:
            assert code in serialized
        assert "sentinel" not in serialized.lower()
