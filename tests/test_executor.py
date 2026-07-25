import httpx
import pytest
from pydantic import ValidationError

from app.schemas import (
    AssertionRule,
    AssertionType,
    HttpMethod,
    TestCase as ApiTestCase,
)
from app.services.executor import TestExecutor as ApiTestExecutor
from app.services.executor import read_json_path


def test_read_json_path_supports_objects_and_lists() -> None:
    document = {"data": {"users": [{"id": 42}]}}

    assert read_json_path(document, "data.users.0.id") == 42


@pytest.mark.parametrize(
    ("path", "document"),
    [
        ("data.missing", {"data": {}}),
        ("data.users.2", {"data": {"users": [{"id": 1}]}}),
        ("data.users.not-an-index", {"data": {"users": [{"id": 1}]}}),
    ],
)
def test_read_json_path_rejects_missing_or_invalid_segments(
    path: str,
    document: object,
) -> None:
    with pytest.raises(KeyError):
        read_json_path(document, path)


def test_json_equals_requires_a_path() -> None:
    with pytest.raises(ValidationError):
        AssertionRule(type=AssertionType.JSON_EQUALS, expected=42)


@pytest.mark.parametrize(
    "method",
    [
        HttpMethod.GET,
        HttpMethod.POST,
        HttpMethod.PUT,
        HttpMethod.PATCH,
        HttpMethod.DELETE,
    ],
)
def test_executor_propagates_request_fields(method: HttpMethod) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method.value
        assert request.url.path == "/items/42"
        assert request.url.params["expand"] == "owner"
        assert request.headers["x-run-id"] == "synthetic-run"
        assert request.read() == b'{"name":"synthetic-item"}'
        return httpx.Response(204)

    case = ApiTestCase(
        name=f"{method.value} item",
        method=method,
        path="/items/42",
        headers={"x-run-id": "synthetic-run"},
        query={"expand": "owner"},
        json_body={"name": "synthetic-item"},
        assertions=[
            AssertionRule(type=AssertionType.STATUS_CODE, expected=204),
        ],
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [case],
    )

    assert result.passed is True


def test_executor_reports_passing_assertions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/42"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"id": 42, "active": True}},
        )

    case = ApiTestCase(
        name="get user",
        method=HttpMethod.GET,
        path="/users/42",
        assertions=[
            AssertionRule(type=AssertionType.STATUS_CODE, expected=200),
            AssertionRule(
                type=AssertionType.JSON_EQUALS,
                path="data.id",
                expected=42,
            ),
            AssertionRule(type=AssertionType.RESPONSE_TIME_MS, expected=1000),
        ],
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [case],
    )

    assert result.passed is True
    assert result.passed_count == 1
    assert all(item.passed for item in result.cases[0].assertions)


def test_executor_reports_a_failed_json_assertion() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"id": 7}})
    )
    case = ApiTestCase(
        name="wrong user id",
        method=HttpMethod.GET,
        path="/users/7",
        assertions=[
            AssertionRule(
                type=AssertionType.JSON_EQUALS,
                path="data.id",
                expected=8,
            )
        ],
    )

    result = ApiTestExecutor(transport=transport).run(
        "https://example.test",
        [case],
    )

    assert result.passed is False
    assert result.failed_count == 1
    assert result.cases[0].assertions[0].actual == 7


def test_executor_handles_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    case = ApiTestCase(
        name="unavailable service",
        method=HttpMethod.GET,
        path="/health",
        assertions=[
            AssertionRule(type=AssertionType.STATUS_CODE, expected=200)
        ],
    )

    result = ApiTestExecutor(transport=httpx.MockTransport(handler)).run(
        "https://example.test",
        [case],
    )

    assert result.passed is False
    assert "ConnectError" in (result.cases[0].error or "")
