from __future__ import annotations

import json
import re
from pathlib import Path
import subprocess

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


def test_evaluation_controls_and_dialog_have_accessible_dom_contract() -> None:
    html = client.get("/").text

    for control_id in ("evaluate-ai-candidates", "evaluate-draft-ai"):
        assert re.search(
            rf'<button\b[^>]*\bid=["\']{control_id}["\'][^>]*'
            r'\btype=["\']button["\']',
            html,
        )
    assert re.search(
        r'<dialog\b[^>]*\bid=["\']ai-feedback-dialog["\'][^>]*'
        r'\baria-labelledby=["\']ai-feedback-title["\']',
        html,
    )
    for output_id in (
        "ai-feedback-title",
        "ai-feedback-scope",
        "ai-feedback-status",
        "ai-feedback-summary",
        "ai-feedback-score",
        "ai-feedback-strengths",
        "ai-feedback-issues",
        "ai-feedback-recommendations",
    ):
        assert re.search(rf'\bid=["\']{output_id}["\']', html)


def test_evaluation_uses_same_origin_post_endpoints_and_never_runs_cases() -> None:
    javascript = javascript_source()
    candidate = function_block(javascript, "evaluateCandidates")
    draft = function_block(javascript, "evaluateCurrentDraft")

    for block, endpoint in (
        (candidate, "/api/v1/ai/cases/evaluate"),
        (draft, "/api/v1/ai/drafts/evaluate"),
    ):
        assert f'fetch("{endpoint}"' in block
        assert re.search(r"method\s*:\s*[\"']POST[\"']", block)
        assert 'credentials: "same-origin"' in block
        assert "validateAiEvaluationResponse(" in block
        assert "/api/v1/runs" not in block
        assert "runTests(" not in block
        assert re.search(r"\bpayload\s*=", block) is None


def test_ai_evaluation_response_validator_rejects_contract_boundary_errors() -> None:
    javascript = javascript_source()
    validator = function_block(javascript, "validateAiEvaluationResponse")
    definition = "function validateAiEvaluationResponse(data, role, allowedCaseIds) {" + validator
    valid = {
        "role": "candidate_evaluator",
        "provider": "mock",
        "model": None,
        "score": 80,
        "summary": "Synthetic summary",
        "strengths": ["clear"],
        "issues": [{"case_id": "case_1", "severity": "warning", "title": "t", "detail": "d", "suggestion": "s"}],
        "recommendations": ["review"],
        "evaluated_case_count": 1,
        "requires_human_review": True,
    }
    variants = {
        "score_low": {"score": -1},
        "score_high": {"score": 101},
        "severity": {"issues": [{"case_id": "case_1", "severity": "critical", "title": "t", "detail": "d", "suggestion": "s"}]},
        "unknown_case": {"issues": [{"case_id": "unknown", "severity": "info", "title": "t", "detail": "d", "suggestion": "s"}]},
        "count": {"evaluated_case_count": 2},
        "role": {"role": "draft_evaluator"},
        "human_review": {"requires_human_review": False},
    }
    program = (
        '"use strict";\n' + definition + "\n" +
        f"const valid = {json.dumps(valid)};\nconst variants = {json.dumps(variants)};\n" +
        "function attempt(value) { try { validateAiEvaluationResponse(value, 'candidate_evaluator', new Set(['case_1'])); return true; } catch (_error) { return false; } }\n" +
        "const outcomes = {};\n"
        "for (const [key, value] of Object.entries(variants)) { outcomes[key] = attempt({...valid, ...value}); }\n"
        "process.stdout.write(JSON.stringify({valid: attempt(valid), variants: outcomes}));\n"
    )
    completed = subprocess.run(["node", "-"], input=program, text=True, encoding="utf-8", capture_output=True, check=False, cwd=repository_root)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["valid"] is True
    assert all(value is False for value in result["variants"].values())


def test_ai_evaluation_validator_counts_unicode_code_points() -> None:
    javascript = javascript_source()
    validator = function_block(javascript, "validateAiEvaluationResponse")
    definition = "function validateAiEvaluationResponse(data, role, allowedCaseIds) {" + validator
    program = (
        '"use strict";\n'
        + definition
        + "\n"
        + "const value = {"
        + 'role: "candidate_evaluator", provider: "mock", model: null, '
        + 'score: 80, summary: "😀".repeat(500), strengths: [], issues: [], '
        + 'recommendations: [], evaluated_case_count: 1, requires_human_review: true};\n'
        + "validateAiEvaluationResponse(value, 'candidate_evaluator', new Set(['case_1']));\n"
        + 'process.stdout.write("accepted");\n'
    )
    completed = subprocess.run(
        ["node", "-"],
        input=program,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        cwd=repository_root,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "accepted"


def test_candidate_generation_auto_evaluates_and_keeps_candidates_on_failure() -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateAiCases")
    assert 'registerReviewSource("ai", result.run, result.insights)' in generator
    assert re.search(r"evaluateCandidates\s*\(.*manual\s*:\s*false", generator, re.DOTALL)


def test_feedback_rendering_uses_safe_dom_text_and_stale_draft_is_not_evaluated_per_input() -> None:
    javascript = javascript_source()
    render = function_block(javascript, "renderAiFeedback")
    stale = function_block(javascript, "markDraftEvaluationStale")
    assert "replaceChildren" in render
    assert "makeElement(" in render
    assert "textContent" in render
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
        assert forbidden not in render
    assert "fetch(" not in stale
    assert "evaluateCurrentDraft(" not in stale
    assert "markDraftEvaluationStale(" in javascript


def test_feedback_window_can_reopen_switch_and_show_stale_timestamp() -> None:
    javascript = javascript_source()
    html = client.get("/").text

    for control_id in (
        "view-ai-candidate-feedback",
        "view-draft-ai-feedback",
        "show-candidate-ai-feedback",
        "show-draft-ai-feedback",
    ):
        assert re.search(
            rf'<button\b[^>]*\bid=["\']{control_id}["\'][^>]*'
            r'\btype=["\']button["\']',
            html,
        )
    for output_id in ("ai-feedback-time", "ai-feedback-stale"):
        assert re.search(rf'\bid=["\']{output_id}["\']', html)

    assert "latestAiFeedback" in javascript
    assert "openLatestAiFeedback" in javascript
    assert "evaluatedAt: new Date().toISOString()" in javascript
    stale = function_block(javascript, "markDraftEvaluationStale")
    assert "draftEvaluationRequestSequence += 1" in stale
    assert "setDraftEvaluationLoading(false)" in stale
    assert "renderAiFeedback(" in stale


def test_editing_candidate_marks_candidate_feedback_stale() -> None:
    javascript = javascript_source()
    update = function_block(javascript, "updateCandidateValue")
    stale = function_block(javascript, "markCandidateEvaluationStale")
    render = function_block(javascript, "renderAiFeedback")

    assert "markCandidateEvaluationStale" in update
    assert re.search(r'candidate\.source\s*===\s*["\']ai["\']', update)
    assert "candidateEvaluationRequestSequence += 1" in stale
    assert "candidateEvaluationIsStale = true" in stale
    assert "candidate_evaluation" in render


def test_programmatic_openapi_updates_invalidate_draft_evaluation() -> None:
    javascript = javascript_source()
    setter = function_block(javascript, "setOpenApiEditorValue")
    demo = function_block(javascript, "loadOpenApiDemo")
    file_handler = function_block(javascript, "handleOpenApiFile")

    assert "elements.openApiEditor.value = value" in setter
    assert "markDraftEvaluationStale" in setter
    assert "setOpenApiEditorValue(" in demo
    assert "setOpenApiEditorValue(" in file_handler
