from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.test_openapi_console import function_block


client = TestClient(app)
repository_root = Path(__file__).resolve().parent.parent


def javascript_source() -> str:
    return (repository_root / "frontend" / "app.js").read_text(encoding="utf-8")


def test_ai_section_exposes_accessible_manual_review_controls() -> None:
    html = client.get("/").text

    assert re.search(
        r"""<section\b[^>]*\bid=["']ai-assistant["'][^>]*"""
        r"""\baria-labelledby=["'][^"']+["']""",
        html,
    )
    for control_id in ("ai-objective", "ai-max-cases"):
        assert re.search(rf"""<label\b[^>]*\bfor=["']{control_id}["']""", html)
        assert re.search(rf"""\bid=["']{control_id}["']""", html)
    for button_id in ("generate-ai", "apply-ai-run"):
        assert re.search(
            rf"""<button\b[^>]*\bid=["']{button_id}["'][^>]*"""
            r"""\btype=["']button["']""",
            html,
        )
    assert re.search(
        r"""<[^>]+\bid=["']ai-status["'][^>]*"""
        r"""\brole=["']status["'][^>]*"""
        r"""\baria-live=["']polite["']""",
        html,
    )
    assert "密钥" in html
    assert not re.search(
        r"""<input\b[^>]*(?:api[-_]?key|password|token)""",
        html,
        re.IGNORECASE,
    )


def test_ai_generation_is_same_origin_and_navigation_never_executes() -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateAiCases")
    apply_block = function_block(javascript, "applyAiRun")

    assert 'fetch("/api/v1/ai/cases/generate"' in generator
    assert 'credentials: "same-origin"' in generator
    assert "validateAiResponse(data)" in generator
    assert re.search(r"\bgeneratedAiRun\s*=", generator)

    assert "candidate-review-title" in apply_block
    assert "scrollIntoView" in apply_block
    assert re.search(r"\bpayload\s*=", apply_block) is None
    assert "fetch(" not in apply_block
    assert "/api/v1/runs" not in apply_block
    assert "runTests(" not in apply_block


def test_ai_response_validation_rejects_credentials_and_automatic_chains() -> None:
    validation = function_block(javascript_source(), "validateAiResponse")

    assert "Object.keys(testCase.headers).length === 0" in validation
    assert "testCase.depends_on.length === 0" in validation
    assert "testCase.extract.length === 0" in validation
    assert "!testCase.path.startsWith(\"//\")" in validation
    assert "!testCase.path.includes(\"://\")" in validation
    assert "run.secret_variables.length !== 0" in validation
    assert "data.requires_human_review !== true" in validation


def test_ai_rendering_uses_text_nodes_and_never_html_interpolation() -> None:
    javascript = javascript_source()
    render = function_block(javascript, "renderAiResult")

    assert "makeElement(" in render
    assert "textContent" in render
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
        assert forbidden not in render


def test_ai_input_changes_invalidate_stale_candidates() -> None:
    javascript = javascript_source()

    assert "aiAbortController.abort()" in javascript
    assert "aiRequestSequence += 1" in javascript
    assert "invalidateGeneratedAi(" in javascript
    assert re.search(
        r"\[\s*elements\.aiObjective,\s*elements\.aiMaxCases\s*\]",
        javascript,
    )
