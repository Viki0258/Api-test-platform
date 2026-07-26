from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_run_history_store
from app.schemas import (
    AssertionResult,
    AssertionType,
    CaseResult,
    CaseStatus,
    TestRunResult as ApiTestRunResult,
)
from app.services.report_renderer import (
    REPORT_CONTENT_SECURITY_POLICY,
    render_test_run_report,
)
from app.services.run_history import HistoryStorageError


RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MISSING_RUN_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EXPECTED_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
repository_root = Path(__file__).resolve().parent.parent
frontend_directory = repository_root / "frontend"


def make_report_result(
    *,
    malicious: str = "synthetic assertion",
    long_value: str = "bounded",
) -> ApiTestRunResult:
    return ApiTestRunResult(
        run_id=RUN_ID,
        created_at=datetime(2026, 7, 26, 1, 2, 3, tzinfo=timezone.utc),
        passed=False,
        total=2,
        passed_count=0,
        failed_count=1,
        skipped_count=1,
        duration_ms=18.25,
        cases=[
            CaseResult(
                id="failed_case",
                name="失败用例 · Unicode 雪豹",
                status=CaseStatus.FAILED,
                passed=False,
                status_code=500,
                response_time_ms=8.5,
                assertions=[
                    AssertionResult(
                        type=AssertionType.JSON_EQUALS,
                        passed=False,
                        expected={
                            "message": malicious,
                            "long": long_value,
                        },
                        actual=["实际值", malicious],
                        message=f"断言失败：{malicious}",
                    )
                ],
                error_code="SYNTHETIC_FAILURE",
                error=f"合成错误：{malicious}",
                extracted_variables=["safe_variable_name"],
            ),
            CaseResult(
                id="skipped_case",
                name="跳过用例",
                status=CaseStatus.SKIPPED,
                passed=False,
                response_time_ms=0,
                assertions=[],
                error_code="DEPENDENCY_FAILED",
                skip_reason="前置用例未通过",
            ),
        ],
    )


class ReportStore:
    def __init__(
        self,
        result: ApiTestRunResult | None,
        *,
        fail: bool = False,
    ) -> None:
        self.result = result
        self.fail = fail
        self.get_calls: list[UUID] = []
        self.list_called = False
        self.save_called = False

    def get(self, run_id: UUID) -> ApiTestRunResult | None:
        self.get_calls.append(run_id)
        if self.fail:
            raise HistoryStorageError(
                "raw sqlite error at C:\\synthetic-sensitive-path"
            )
        if self.result is not None and self.result.run_id == run_id:
            return self.result
        return None

    def list(self, _limit: int):
        self.list_called = True
        raise AssertionError("report endpoint must not list history")

    def save(self, _result: ApiTestRunResult) -> None:
        self.save_called = True
        raise AssertionError("report endpoint must not write history")


@pytest.fixture
def report_client():
    store = ReportStore(make_report_result())
    app.dependency_overrides[get_run_history_store] = lambda: store
    try:
        with TestClient(app) as client:
            yield client, store
    finally:
        app.dependency_overrides.clear()


def test_report_endpoint_downloads_utf8_html_with_stable_headers(
    report_client,
) -> None:
    client, store = report_client

    response = client.get(f"/api/v1/runs/{RUN_ID}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].lower() == (
        "text/html; charset=utf-8"
    )
    assert response.headers["content-disposition"] == (
        f'attachment; filename="api-test-report-{RUN_ID}.html"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert REPORT_CONTENT_SECURITY_POLICY == EXPECTED_CSP
    assert response.content.decode("utf-8") == response.text
    assert "Unicode 雪豹" in response.text
    assert store.get_calls == [RUN_ID]
    assert store.list_called is False
    assert store.save_called is False


@pytest.mark.parametrize(
    "run_id",
    [
        "not-a-uuid",
        "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
        str(MISSING_RUN_ID),
    ],
)
def test_report_invalid_non_v4_and_missing_ids_share_stable_404(
    report_client,
    run_id: str,
) -> None:
    client, _store = report_client

    response = client.get(f"/api/v1/runs/{run_id}/report")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_report_storage_failure_returns_sanitized_stable_503() -> None:
    store = ReportStore(None, fail=True)
    app.dependency_overrides[get_run_history_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/runs/{RUN_ID}/report")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "HISTORY_STORAGE_UNAVAILABLE"
    )
    assert "sqlite" not in response.text.lower()
    assert "synthetic-sensitive-path" not in response.text


def test_report_has_strict_meta_csp_and_no_active_or_external_content() -> None:
    html = render_test_run_report(make_report_result())
    lowered = html.lower()

    meta = re.search(
        r"""<meta\b[^>]*http-equiv=(?P<http_quote>["'])"""
        r"""content-security-policy(?P=http_quote)[^>]*"""
        r"""content=(?P<content_quote>["'])(?P<csp>.*?)"""
        r"""(?P=content_quote)[^>]*>""",
        html,
        re.IGNORECASE,
    )
    assert meta is not None
    meta_csp = unescape(meta.group("csp"))
    for directive in (
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in meta_csp

    for forbidden in (
        "<script",
        "<form",
        "<a ",
        "<link",
        "<img",
        "<iframe",
        "javascript:",
        "data:",
        "http://",
        "https://",
        "src=",
        "href=",
        "url(",
    ):
        assert forbidden not in lowered


def test_report_escapes_xss_preserves_unicode_and_truncates_long_values() -> None:
    malicious = (
        '<script>alert("synthetic")</script>'
        '<img src=x onerror="alert(1)">'
        '<a href="https://evil.example.test">click</a>'
    )
    long_value = "长" + ("x" * 2500) + "尾"

    html = render_test_run_report(
        make_report_result(
            malicious=malicious,
            long_value=long_value,
        )
    )

    assert malicious not in html
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "<a " not in html.lower()
    assert "&lt;script&gt;" in html
    assert "Unicode 雪豹" in html
    assert "实际值" in html
    assert long_value not in html
    longest_x_run = max(
        (len(match.group(0)) for match in re.finditer(r"x+", html)),
        default=0,
    )
    assert longest_x_run <= 2000


def test_report_excludes_all_forbidden_request_and_response_fields() -> None:
    html = render_test_run_report(make_report_result()).lower()

    for forbidden in (
        "base_url",
        "request_headers",
        "request_query",
        "request_body",
        "json_body",
        "variable_values",
        "secret_variables",
        "original_responses",
        "authorization",
        "cookie",
    ):
        assert forbidden not in html


def test_frontend_builds_report_link_only_for_canonical_uuid4() -> None:
    javascript = (frontend_directory / "app.js").read_text(encoding="utf-8")
    renderer_start = javascript.index("function renderHistoryItem")
    renderer_end = javascript.index("function renderHistoryList", renderer_start)
    renderer = javascript[renderer_start:renderer_end]

    assert "下载报告" in renderer
    assert re.search(
        r"""encodeURIComponent\s*\(\s*[^)]*run_id[^)]*\)""",
        renderer,
        re.IGNORECASE,
    )
    assert re.search(
        r"""`/api/v1/runs/\$\{[^}]+\}/report`""",
        renderer,
    )
    assert re.search(
        r"""(?:is|validate|canonical)[A-Za-z0-9_]*Uuid"""
        r"""[A-Za-z0-9_]*\s*\(""",
        renderer,
        re.IGNORECASE,
    )
    assert re.search(
        r"""makeElement\s*\(\s*["']a["']""",
        renderer,
    )
    assert re.search(r"\.href\s*=", renderer)
    assert not re.search(r"\.target\s*=", renderer)


def test_frontend_invalid_history_id_has_no_link_and_detail_still_works() -> None:
    javascript = (frontend_directory / "app.js").read_text(encoding="utf-8")
    renderer_start = javascript.index("function renderHistoryItem")
    renderer_end = javascript.index("function renderHistoryList", renderer_start)
    renderer = javascript[renderer_start:renderer_end]

    assert re.search(
        r"""if\s*\(\s*(?:is|validate|canonical)[A-Za-z0-9_]*Uuid"""
        r"""[A-Za-z0-9_]*\s*\(""",
        renderer,
        re.IGNORECASE,
    )
    assert re.search(
        r"""button\.addEventListener\s*\(\s*["']click["']"""
        r"""(?:(?!\}\s*\)).)*loadHistoryDetail\s*\(""",
        renderer,
        re.DOTALL,
    )
    assert "listItem.append(button)" in renderer


def test_frontend_report_export_uses_no_unsafe_navigation_or_blob_api() -> None:
    javascript = (frontend_directory / "app.js").read_text(encoding="utf-8")

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "Blob(",
        "URL.createObjectURL",
        "webkitURL",
        "window.open",
        "location.href",
        "location.assign",
        "location.replace",
    ):
        assert forbidden not in javascript
    assert not re.search(
        r"""(?:href|target)\s*=\s*["']https?://""",
        javascript,
    )
