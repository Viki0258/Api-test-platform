from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_ai_assistant
from app.schemas import AiGenerateRequest
from app.services.ai_assistant import (
    AiAssistantService,
    AiAssistantError,
    CandidateQueryParameter,
    MockAiProvider,
    OpenAiProvider,
    ProviderCandidate,
    ProviderOutput,
    _boundary_value,
    _contains_sensitive_key,
    _matching_template,
    _preferred_success_status,
    _request_body_schema,
    _response_output_text,
    _response_statuses,
    _safe_sample,
    _safe_schema,
    _safe_text,
    _validate_candidates,
    build_safe_outline,
)
from tests.test_openapi_generator import document, operation


client = TestClient(app)


def ai_document(*, secret: str = "synthetic-secret-never-send") -> dict:
    return document(
        {
            "/users/{user_id}": {
                "get": operation(
                    "get_user",
                    summary="Query a synthetic user",
                    security=[{"BearerAuth": []}],
                    parameters=[
                        {
                            "name": "user_id",
                            "in": "path",
                            "required": True,
                            "example": secret,
                            "schema": {
                                "type": "integer",
                                "example": 7,
                                "default": 8,
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": True,
                            "example": secret,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 20,
                            },
                        },
                        {
                            "name": "Authorization",
                            "in": "header",
                            "required": True,
                            "example": secret,
                            "schema": {"type": "string"},
                        },
                    ],
                )
            }
        },
        components={
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": secret,
                }
            }
        },
    )


def test_mock_status_and_generation_require_no_key_or_network() -> None:
    status = client.get("/api/v1/ai/status")
    assert status.status_code == 200
    assert status.json() == {
        "provider": "mock",
        "configured": True,
        "model": None,
        "network_access": False,
    }

    response = client.post(
        "/api/v1/ai/cases/generate",
        json={
            "document": ai_document(),
            "objective": "生成参数边界候选用例",
            "max_cases": 3,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["requires_human_review"] is True
    assert body["generated_count"] == 1
    assert len(body["insights"]) == 1
    generated = body["run"]["cases"][0]
    assert generated["headers"] == {}
    assert generated["depends_on"] == []
    assert generated["extract"] == []
    assert generated["query"]["limit"] == -1
    assert generated["assertions"][0]["expected"] == 400


def test_safe_outline_strips_values_authentication_and_servers() -> None:
    secret = "synthetic-secret-never-send"
    outline = build_safe_outline(ai_document(secret=secret))
    serialized = json.dumps(outline, ensure_ascii=False)

    assert secret not in serialized
    assert "securitySchemes" not in serialized
    assert "security" not in serialized
    assert "Authorization" not in serialized
    assert "servers" not in serialized
    assert "example" not in serialized
    assert "default" not in serialized
    assert '"minimum": 1' in serialized
    assert '"maximum": 100' in serialized


def test_openai_status_is_available_without_key_and_generation_is_blocked() -> None:
    service = AiAssistantService(Settings(ai_provider="openai"))
    app.dependency_overrides[get_ai_assistant] = lambda: service
    try:
        status = client.get("/api/v1/ai/status")
        response = client.post(
            "/api/v1/ai/cases/generate",
            json={"document": ai_document()},
        )
    finally:
        app.dependency_overrides.clear()

    assert status.status_code == 200
    assert status.json()["configured"] is False
    assert status.json()["network_access"] is True
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


def test_openai_provider_uses_structured_responses_without_storing_input() -> None:
    secret = "synthetic-secret-never-send"
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        provider_result = {
            "candidates": [
                {
                    "id": "ai_limit_boundary",
                    "name": "Limit lower boundary",
                    "rationale": "Exercise the declared lower bound.",
                    "category": "boundary",
                    "method": "GET",
                    "path": "/users/7",
                    "query": [{"name": "limit", "value": 0}],
                    "json_body_json": None,
                    "expected_status": 400,
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(provider_result),
                            }
                        ],
                    }
                ]
            },
        )

    provider = OpenAiProvider(
        api_key=secret,
        model="synthetic-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    service = AiAssistantService(
        Settings(
            ai_provider="openai",
            openai_api_key=secret,
            openai_model="synthetic-model",
        ),
        provider=provider,
    )
    result = service.generate(
        AiGenerateRequest(
            document=ai_document(secret=secret),
            objective="Generate a boundary case",
        )
    )

    request_body = captured["body"]
    assert captured["authorization"] == f"Bearer {secret}"
    assert request_body["store"] is False
    assert request_body["text"]["format"]["type"] == "json_schema"
    assert request_body["text"]["format"]["strict"] is True
    assert secret not in request_body["input"]
    assert result.run.cases[0].headers == {}
    assert result.run.cases[0].query == {"limit": 0}


def test_invalid_provider_candidate_cannot_add_unknown_query_parameter() -> None:
    class UnsafeProvider:
        name = "synthetic"
        model = "synthetic-model"

        def generate(self, **_kwargs):
            from app.services.ai_assistant import (
                CandidateQueryParameter,
                ProviderCandidate,
                ProviderOutput,
            )

            return ProviderOutput(
                candidates=[
                    ProviderCandidate(
                        id="unsafe_case",
                        name="Unsafe candidate",
                        rationale="Attempts to invent a credential parameter.",
                        category="negative",
                        method="GET",
                        path="/users/7",
                        query=[
                            CandidateQueryParameter(
                                name="api_key",
                                value="synthetic",
                            )
                        ],
                        json_body_json=None,
                        expected_status=401,
                    )
                ]
            )

    service = AiAssistantService(Settings(), provider=UnsafeProvider())
    app.dependency_overrides[get_ai_assistant] = lambda: service
    try:
        response = client.post(
            "/api/v1/ai/cases/generate",
            json={"document": ai_document()},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "AI_PROVIDER_INVALID_OUTPUT"
    assert "synthetic" not in response.text


def test_invalid_ai_input_returns_stable_non_echoing_error() -> None:
    secret = "synthetic-invalid-document-secret"
    response = client.post(
        "/api/v1/ai/cases/generate",
        json={
            "document": {
                "openapi": "2.0",
                "x-secret": secret,
                "paths": {},
            }
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_AI_SOURCE_DOCUMENT"
    assert secret not in response.text


def test_mock_generates_negative_body_and_robustness_candidates() -> None:
    spec = document(
        {
            "/items": {
                "post": operation(
                    "create_item",
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "count": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    },
                    responses={"201": {"description": "created"}},
                )
            },
            "/health": {
                "get": {
                    "responses": {"204": {"description": "healthy"}},
                }
            },
        }
    )

    response = client.post(
        "/api/v1/ai/cases/generate",
        json={"document": spec, "max_cases": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["category"] for item in body["insights"]] == [
        "negative",
        "robustness",
    ]
    assert body["run"]["cases"][0]["json_body"] == {"name": None}
    assert body["run"]["cases"][0]["assertions"][0]["expected"] == 400
    assert body["run"]["cases"][1]["assertions"][0]["expected"] == 204


@pytest.mark.parametrize(
    ("document_value", "expected_code"),
    [
        ({"openapi": "3.0.3", "paths": []}, "INVALID_AI_SOURCE_DOCUMENT"),
        (
            {"openapi": "3.0.3", "paths": {"not-relative": {"get": {}}}},
            "NO_AI_SOURCE_OPERATIONS",
        ),
        (
            {
                "openapi": "3.0.3",
                "paths": {
                    f"/item-{index}": {}
                    for index in range(201)
                },
            },
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
        ),
    ],
)
def test_safe_outline_rejects_invalid_or_oversized_shapes(
    document_value,
    expected_code: str,
) -> None:
    with pytest.raises(AiAssistantError) as captured:
        build_safe_outline(document_value)
    assert captured.value.code == expected_code


def test_safe_outline_rejects_non_serializable_and_oversized_documents() -> None:
    with pytest.raises(AiAssistantError) as non_serializable:
        build_safe_outline({"openapi": "3.0.3", "paths": {}, "bad": {1, 2}})
    assert non_serializable.value.code == "INVALID_AI_SOURCE_DOCUMENT"

    with pytest.raises(AiAssistantError) as oversized:
        build_safe_outline(
            {
                "openapi": "3.0.3",
                "paths": {},
                "padding": "x" * 1_048_576,
            }
        )
    assert oversized.value.code == "AI_SOURCE_DOCUMENT_TOO_LARGE"


def test_openai_provider_maps_transport_and_invalid_json_failures() -> None:
    outline = build_safe_outline(ai_document())

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "synthetic"})

    provider = OpenAiProvider(
        api_key="synthetic",
        model="synthetic-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(unavailable),
    )
    with pytest.raises(AiAssistantError) as unavailable_error:
        provider.generate(
            outline=outline,
            objective="synthetic",
            max_cases=1,
        )
    assert unavailable_error.value.code == "AI_PROVIDER_UNAVAILABLE"

    def invalid_output(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "{}"})

    provider = OpenAiProvider(
        api_key="synthetic",
        model="synthetic-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(invalid_output),
    )
    with pytest.raises(AiAssistantError) as invalid_error:
        provider.generate(
            outline=outline,
            objective="synthetic",
            max_cases=1,
        )
    assert invalid_error.value.code == "AI_PROVIDER_INVALID_OUTPUT"


def test_candidate_validation_rejects_unsafe_body_duplicate_and_unknown_paths() -> None:
    outline = build_safe_outline(ai_document())

    def candidate(**changes) -> ProviderCandidate:
        values = {
            "id": "candidate_1",
            "name": "Synthetic candidate",
            "rationale": "Synthetic rationale",
            "category": "negative",
            "method": "GET",
            "path": "/users/7",
            "query": [CandidateQueryParameter(name="limit", value=0)],
            "json_body_json": None,
            "expected_status": 400,
        }
        values.update(changes)
        return ProviderCandidate(**values)

    invalid_outputs = [
        ProviderOutput(
            candidates=[
                candidate(),
                candidate(name="Duplicate"),
            ]
        ),
        ProviderOutput(candidates=[candidate(path="/unknown")]),
        ProviderOutput(
            candidates=[
                candidate(
                    query=[
                        CandidateQueryParameter(name="limit", value=0),
                        CandidateQueryParameter(name="limit", value=1),
                    ]
                )
            ]
        ),
        ProviderOutput(candidates=[candidate(json_body_json="{not-json")]),
        ProviderOutput(
            candidates=[
                candidate(json_body_json='{"nested":{"api_key":"synthetic"}}')
            ]
        ),
        ProviderOutput(
            candidates=[candidate(json_body_json=json.dumps("x" * 65_537))]
        ),
    ]
    for output in invalid_outputs:
        with pytest.raises(AiAssistantError) as captured:
            _validate_candidates(output, outline=outline, max_cases=10)
        assert captured.value.code == "AI_PROVIDER_INVALID_OUTPUT"


def test_candidate_limit_returns_a_structured_warning() -> None:
    outline = build_safe_outline(ai_document())
    output = ProviderOutput(
        candidates=[
            ProviderCandidate(
                id=f"candidate_{index}",
                name=f"Candidate {index}",
                rationale="Synthetic rationale",
                category="boundary",
                method="GET",
                path="/users/7",
                query=[CandidateQueryParameter(name="limit", value=index)],
                json_body_json=None,
                expected_status=400,
            )
            for index in range(1, 3)
        ]
    )

    cases, insights, warnings = _validate_candidates(
        output,
        outline=outline,
        max_cases=1,
    )

    assert len(cases) == len(insights) == 1
    assert warnings[0].code == "AI_CASE_LIMIT_APPLIED"


def test_sanitizer_and_provider_helpers_cover_defensive_shapes() -> None:
    assert _safe_schema(None, depth=0) is None
    assert _safe_schema({"$ref": "#/components/schemas/Item"}, depth=0) == {
        "type": "referenced"
    }
    schema = _safe_schema(
        {
            "type": "array",
            "format": "synthetic",
            "enum": ["secret-value"],
            "required": ["safe", "token"],
            "items": {"type": "number"},
            "properties": {
                "safe": {"type": "boolean"},
                "password": {"type": "string"},
            },
        },
        depth=0,
    )
    assert schema["enum_count"] == 1
    assert schema["required"] == ["safe"]
    assert set(schema["properties"]) == {"safe"}
    assert schema["items"] == {"type": "number"}
    assert _request_body_schema(None) is None
    assert _request_body_schema({"content": []}) is None
    assert _request_body_schema({"content": {"application/json": {}}}) is None
    assert _response_statuses(None) == []
    assert _response_statuses({"200": {}, 404: {}, None: {}}) == ["200", "404"]
    assert _safe_text(None, 5) is None
    assert _safe_text("123456", 5) == "12345"
    assert _matching_template(
        "POST",
        "/users/7",
        {("GET", "/users/{id}"): set()},
    ) is None
    assert _response_output_text({"output_text": "synthetic"}) == "synthetic"
    with pytest.raises(AiAssistantError):
        _response_output_text(
            {
                "output": [
                    None,
                    {"content": None},
                    {"content": [None, {"type": "refusal"}]},
                ]
            }
        )
    assert _contains_sensitive_key([{"safe": ["value"]}]) is False
    assert _contains_sensitive_key([{"session": "synthetic"}]) is True
    assert _boundary_value(True) is False
    assert _boundary_value(1.5) == -1
    assert _boundary_value("value") == ""
    assert _safe_sample(None) == "sample"
    assert _safe_sample({"type": "integer"}) == 1
    assert _safe_sample({"type": "number"}) == 1.0
    assert _safe_sample({"type": "boolean"}) is True
    assert _safe_sample({"type": "array", "items": {"type": "integer"}}) == [1]
    assert _safe_sample(
        {
            "type": "object",
            "properties": {
                "required": {"type": "string"},
                "optional": {"type": "integer"},
            },
            "required": ["required"],
        }
    ) == {"required": "sample"}
    assert _preferred_success_status(["default", "404"]) == 200
