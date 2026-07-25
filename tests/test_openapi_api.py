from __future__ import annotations

import logging
import socket
import urllib.request

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.services.run_history import RunHistoryStore
from tests.test_openapi_generator import document, operation


client = TestClient(app)


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def test_request_base_url_overrides_the_first_document_server() -> None:
    response = client.post(
        "/api/v1/openapi/generate",
        json={
            "document": document(
                {
                    "/health": {
                        "get": operation("health"),
                    }
                },
                server_url="https://document.example.test/v1",
            ),
            "base_url": "https://override.example.test/api",
        },
    )

    assert response.status_code == 200
    assert response.json()["run"]["base_url"].rstrip("/") == (
        "https://override.example.test/api"
    )


def test_first_document_server_is_used_when_override_is_absent() -> None:
    spec = document(
        {
            "/health": {
                "get": operation("health"),
            }
        },
        server_url="https://first.example.test/v1",
    )
    spec["servers"].append({"url": "https://second.example.test/v2"})

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": spec},
    )

    assert response.status_code == 200
    assert response.json()["run"]["base_url"].rstrip("/") == (
        "https://first.example.test/v1"
    )


@pytest.mark.parametrize(
    ("document_value", "override", "expected_code"),
    [
        (
            document({"/ok": {"get": operation("ok")}}, version="3.2.0"),
            None,
            "INVALID_OPENAPI_VERSION",
        ),
        (
            {
                "swagger": "2.0",
                "info": {"title": "old", "version": "1"},
                "paths": {},
            },
            None,
            "INVALID_OPENAPI_VERSION",
        ),
        (
            document(
                {"/ok": {"get": operation("ok")}},
                server_url="https://user:password@example.test",
            ),
            None,
            "INVALID_OPENAPI_BASE_URL",
        ),
        (
            document(
                {"/ok": {"get": operation("ok")}},
                server_url="https://example.test#fragment",
            ),
            None,
            "INVALID_OPENAPI_BASE_URL",
        ),
        (
            document(
                {"/ok": {"get": operation("ok")}},
                server_url="https://{environment}.example.test",
            ),
            None,
            "INVALID_OPENAPI_BASE_URL",
        ),
        (
            document({"/ok": {"get": operation("ok")}}),
            "ftp://example.test",
            "INVALID_OPENAPI_BASE_URL",
        ),
        (
            document({"/ok": {"get": operation("ok")}}),
            "https://example.test:99999",
            "INVALID_OPENAPI_BASE_URL",
        ),
        (
            {
                "openapi": "3.0.3",
                "info": {"title": "No server", "version": "1"},
                "paths": {"/ok": {"get": operation("ok")}},
            },
            None,
            "OPENAPI_BASE_URL_REQUIRED",
        ),
    ],
)
def test_version_and_base_url_failures_return_stable_codes(
    document_value: dict,
    override: str | None,
    expected_code: str,
) -> None:
    payload = {"document": document_value}
    if override is not None:
        payload["base_url"] = override

    response = client.post("/api/v1/openapi/generate", json=payload)

    assert response.status_code == 422
    assert error_code(response) == expected_code


def test_document_size_limit_is_enforced() -> None:
    spec = document({"/ok": {"get": operation("ok")}})
    spec["x-padding"] = "x" * 1_048_576

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": spec},
    )

    assert response.status_code == 422
    assert error_code(response) == "OPENAPI_DOCUMENT_TOO_LARGE"


def test_path_count_limit_is_enforced() -> None:
    paths = {
        f"/synthetic-{index}": {"get": operation(f"case_{index}")}
        for index in range(201)
    }

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": document(paths)},
    )

    assert response.status_code == 422
    assert error_code(response) == "OPENAPI_TOO_MANY_PATHS"


@pytest.mark.parametrize("max_cases", [0, 51, "not-an-integer"])
def test_max_cases_rejects_out_of_bounds_or_invalid_values(max_cases) -> None:
    response = client.post(
        "/api/v1/openapi/generate",
        json={
            "document": document(
                {"/ok": {"get": operation("ok")}}
            ),
            "max_cases": max_cases,
        },
    )

    assert response.status_code == 422


def test_max_cases_truncates_deterministically_with_warning() -> None:
    response = client.post(
        "/api/v1/openapi/generate",
        json={
            "document": document(
                {
                    "/first": {"get": operation("first")},
                    "/second": {"get": operation("second")},
                }
            ),
            "max_cases": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["generated_count"] == 1
    assert body["skipped_count"] == 1
    assert len(body["run"]["cases"]) == 1
    assert body["warnings"]


def test_no_generatable_operation_returns_stable_422() -> None:
    response = client.post(
        "/api/v1/openapi/generate",
        json={
            "document": document(
                {
                    "/no-success": {
                        "get": operation(
                            "no_success",
                            responses={
                                "400": {"description": "only failure"}
                            },
                        ),
                        "options": operation("unsupported"),
                    }
                }
            )
        },
    )

    assert response.status_code == 422
    assert error_code(response) == "NO_OPENAPI_CASES_GENERATED"


def test_external_reference_never_attempts_network_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("OpenAPI generation attempted network access")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
    synthetic_ref_token = "synthetic-ref-token-do-not-return"
    spec = document(
        {
            "/valid": {"get": operation("valid")},
            "/external": {
                "get": operation(
                    "external",
                    parameters=[
                        {
                            "$ref": (
                                "https://unreachable.example.test/"
                                f"{synthetic_ref_token}/parameter.json"
                                "#/Synthetic"
                            )
                        }
                    ],
                )
            },
        }
    )

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": spec},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["generated_count"] == 1
    assert body["skipped_count"] == 1
    assert body["warnings"]
    assert synthetic_ref_token not in response.text


def test_generation_does_not_execute_log_or_persist_the_source_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_secret = "synthetic-source-secret-do-not-return"

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("generation invoked a forbidden side effect")

    monkeypatch.setattr(socket, "create_connection", forbidden_side_effect)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_side_effect)
    monkeypatch.setattr(RunHistoryStore, "save", forbidden_side_effect)
    monkeypatch.setattr(logging.Logger, "_log", forbidden_side_effect)

    class ForbiddenExecutor:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("generation attempted test execution")

    monkeypatch.setattr(main_module, "TestExecutor", ForbiddenExecutor)
    spec = document(
        {
            "/safe": {
                "get": operation(
                    "safe_generation",
                    security=[{"SyntheticApiKey": []}],
                    parameters=[
                        {
                            "name": "Authorization",
                            "in": "header",
                            "required": True,
                            "example": synthetic_secret,
                            "schema": {"type": "string"},
                        }
                    ],
                )
            }
        },
        components={
            "securitySchemes": {
                "SyntheticApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": synthetic_secret,
                }
            }
        },
    )

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": spec},
    )

    assert response.status_code == 200
    assert synthetic_secret not in response.text
    assert response.json()["run"]["cases"][0]["headers"] == {}


def test_invalid_base_url_detail_does_not_echo_credentials() -> None:
    synthetic_password = "synthetic-url-password-do-not-return"
    spec = document(
        {"/safe": {"get": operation("safe")}},
        server_url=f"https://user:{synthetic_password}@example.test",
    )

    response = client.post(
        "/api/v1/openapi/generate",
        json={"document": spec},
    )

    assert response.status_code == 422
    assert error_code(response) == "INVALID_OPENAPI_BASE_URL"
    assert synthetic_password not in response.text
