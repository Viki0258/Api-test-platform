from __future__ import annotations

import json
from typing import Any

import httpx

from app.schemas import (
    AssertionRule,
    AssertionType,
    ExtractionRule,
    HttpMethod,
    TestCase as ApiTestCase,
)
from app.services.executor import TestExecutor as ApiTestExecutor


def status(expected: int = 200) -> AssertionRule:
    return AssertionRule(type=AssertionType.STATUS_CODE, expected=expected)


def case(
    *,
    name: str,
    path: str,
    case_id: str | None = None,
    depends_on: list[str] | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    json_body: Any = None,
    assertions: list[AssertionRule] | None = None,
    extract: list[ExtractionRule] | None = None,
) -> ApiTestCase:
    return ApiTestCase(
        id=case_id,
        name=name,
        method=HttpMethod.POST,
        path=path,
        depends_on=depends_on or [],
        headers=headers or {},
        query=query or {},
        json_body=json_body,
        assertions=assertions or [status()],
        extract=extract or [],
    )


def test_run_variables_render_all_supported_locations_and_preserve_types() -> None:
    variables = {
        "user_id": 42,
        "token": "synthetic-token",
        "active": False,
        "metadata": {"source": "contract-test"},
        "expected_id": 42,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/42"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.url.params["user_id"] == "42"
        assert request.url.params["label"] == "user-42"
        assert json.loads(request.content) == {
            "id": 42,
            "active": False,
            "metadata": {"source": "contract-test"},
            "label": "user-42",
        }
        return httpx.Response(200, json={"data": {"id": 42}})

    workflow = case(
        name="render variables",
        path="/users/{{user_id}}",
        headers={"Authorization": "Bearer {{token}}"},
        query={"user_id": "{{user_id}}", "label": "user-{{user_id}}"},
        json_body={
            "id": "{{user_id}}",
            "active": "{{active}}",
            "metadata": "{{metadata}}",
            "label": "user-{{user_id}}",
        },
        assertions=[
            status(),
            AssertionRule(
                type=AssertionType.JSON_EQUALS,
                path="data.id",
                expected="{{expected_id}}",
            ),
        ],
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [workflow],
        variables=variables,
    )

    assert result.passed is True
    assert result.cases[0].assertions[1].actual == 42
    assert result.cases[0].assertions[1].expected == 42


def test_missing_variable_fails_without_sending_request() -> None:
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(200)

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [case(name="missing variable", path="/users/{{missing_id}}")],
    )

    assert sent == 0
    assert result.cases[0].status == "failed"
    assert result.cases[0].error_code == "VARIABLE_NOT_FOUND"


def test_embedded_complex_value_fails_without_sending_request() -> None:
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(200)

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [case(name="complex interpolation", path="/items/{{payload}}")],
        variables={"payload": {"id": 1}},
    )

    assert sent == 0
    assert result.cases[0].status == "failed"


def test_response_extraction_supplies_a_dependent_case() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(
                200,
                json={"data": {"token": "synthetic-access-token"}},
            )
        assert request.url.path == "/profile"
        assert (
            request.headers["authorization"]
            == "Bearer synthetic-access-token"
        )
        return httpx.Response(200, json={"data": {"id": 7}})

    login = case(
        case_id="login",
        name="login",
        path="/login",
        extract=[
            ExtractionRule(
                name="access_token",
                path="data.token",
                secret=True,
            )
        ],
    )
    profile = case(
        case_id="profile",
        name="profile",
        path="/profile",
        depends_on=["login"],
        headers={"Authorization": "Bearer {{access_token}}"},
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [login, profile],
    )

    assert len(requests) == 2
    assert result.passed is True
    assert result.cases[0].extracted_variables == ["access_token"]
    assert "synthetic-access-token" not in result.model_dump_json()


def test_extraction_failure_skips_dependent_but_independent_case_continues() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/producer":
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200)

    producer = case(
        case_id="producer",
        name="producer",
        path="/producer",
        extract=[ExtractionRule(name="item_id", path="data.id")],
    )
    dependent = case(
        case_id="dependent",
        name="dependent",
        path="/items/{{item_id}}",
        depends_on=["producer"],
    )
    independent = case(
        case_id="independent",
        name="independent",
        path="/health",
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [producer, dependent, independent],
    )

    assert requested_paths == ["/producer", "/health"]
    assert result.cases[0].status == "failed"
    assert result.cases[0].error_code == "EXTRACTION_FAILED"
    assert result.cases[1].status == "skipped"
    assert result.cases[1].error_code == "DEPENDENCY_FAILED"
    assert result.cases[2].status == "passed"
    assert result.passed_count == 1
    assert result.failed_count == 1
    assert result.skipped_count == 1


def test_failed_assertion_does_not_publish_extracted_variables() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"data": {"id": 99}})

    producer = case(
        case_id="producer",
        name="failed producer",
        path="/producer",
        assertions=[status(201)],
        extract=[ExtractionRule(name="item_id", path="data.id")],
    )
    dependent = case(
        case_id="dependent",
        name="dependent",
        path="/items/{{item_id}}",
        depends_on=["producer"],
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [producer, dependent],
    )

    assert requested_paths == ["/producer"]
    assert result.cases[0].status == "failed"
    assert result.cases[0].extracted_variables == []
    assert result.cases[1].status == "skipped"


def test_extraction_cannot_overwrite_an_existing_variable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"id": 2}})
    )
    producer = case(
        name="duplicate variable",
        path="/producer",
        extract=[ExtractionRule(name="item_id", path="data.id")],
    )

    result = ApiTestExecutor(transport=transport).run(
        "https://example.test",
        [producer],
        variables={"item_id": 1},
    )

    assert result.cases[0].status == "failed"
    assert result.cases[0].error_code == "EXTRACTION_FAILED"
    assert result.cases[0].extracted_variables == []


def test_secret_values_are_redacted_from_assertions_and_errors() -> None:
    secret = "synthetic-secret-value"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": {"token": "different-value"}},
        )
    )
    workflow = case(
        name="secret assertion",
        path="/token",
        headers={"Authorization": "Bearer {{token}}"},
        assertions=[
            AssertionRule(
                type=AssertionType.JSON_EQUALS,
                path="data.token",
                expected="{{token}}",
            )
        ],
    )

    result = ApiTestExecutor(transport=transport).run(
        "https://example.test",
        [workflow],
        variables={"token": secret},
        secret_variables=["token"],
    )
    serialized = result.model_dump_json()

    assert result.cases[0].status == "failed"
    assert secret not in serialized
    assert "authorization" not in serialized.lower()
    assert "[REDACTED]" in serialized


def test_extracted_variables_do_not_leak_between_runs() -> None:
    executor = ApiTestExecutor(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": {"id": 42}},
            )
        )
    )
    producer = case(
        name="producer",
        path="/producer",
        extract=[ExtractionRule(name="item_id", path="data.id")],
    )
    consumer = case(name="consumer", path="/items/{{item_id}}")

    first = executor.run("https://example.test", [producer])
    second = executor.run("https://example.test", [consumer])

    assert first.cases[0].status == "passed"
    assert second.cases[0].status == "failed"
    assert second.cases[0].error_code == "VARIABLE_NOT_FOUND"
