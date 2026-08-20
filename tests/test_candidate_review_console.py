from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from fastapi.testclient import TestClient

from app.main import app
from tests.test_openapi_console import function_block


client = TestClient(app)
repository_root = Path(__file__).resolve().parent.parent
frontend_directory = repository_root / "frontend"


def javascript_source() -> str:
    return (frontend_directory / "app.js").read_text(encoding="utf-8")


def test_review_workbench_has_accessible_controls_and_no_secret_fields() -> None:
    html = client.get("/").text

    assert re.search(
        r"""<section\b[^>]*\bid=["']candidate-review["'][^>]*"""
        r"""\baria-labelledby=["']candidate-review-title["']""",
        html,
    )
    for control_id in (
        "select-all-candidates",
        "clear-candidate-selection",
        "replace-editor-candidates",
        "append-editor-candidates",
    ):
        assert re.search(
            rf"""<button\b[^>]*\bid=["']{control_id}["'][^>]*"""
            r"""\btype=["']button["']""",
            html,
        )
    assert re.search(
        r"""<[^>]+\bid=["']review-status["'][^>]*"""
        r"""\brole=["']status["'][^>]*"""
        r"""\baria-live=["']polite["']""",
        html,
    )
    review_section = html.split('id="candidate-review"', 1)[1].split(
        '<div class="workspace">',
        1,
    )[0]
    for forbidden in (
        "Authorization",
        "API Key",
        'type="password"',
        "depends_on",
        "extract",
    ):
        assert forbidden not in review_section


def test_both_generators_register_candidates_in_the_shared_workbench() -> None:
    javascript = javascript_source()
    openapi_generator = function_block(javascript, "generateOpenApiCases")
    ai_generator = function_block(javascript, "generateAiCases")
    register = function_block(javascript, "registerReviewSource")

    assert 'registerReviewSource("openapi", result.run, [])' in openapi_generator
    assert (
        'registerReviewSource("ai", result.run, result.insights)'
        in ai_generator
    )
    assert "candidate.source !== source" in register
    assert "reviewCandidates = [...retained, ...additions]" in register
    assert "renderCandidateReview()" in register


def test_review_cards_use_safe_dom_and_only_expose_frozen_editable_fields() -> None:
    render = function_block(javascript_source(), "renderCandidateCard")

    assert "makeElement(" in render
    assert "addEventListener(" in render
    for field in (
        "name",
        "method",
        "path",
        "expectedStatus",
        "queryText",
        "bodyText",
    ):
        assert f'"{field}"' in render
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "headers",
        "depends_on",
        "extract",
    ):
        assert forbidden not in render


def test_review_cards_are_collapsed_accessible_editors_by_default() -> None:
    render = function_block(javascript_source(), "renderCandidateCard")

    assert 'makeElement("details", "candidate-card")' in render
    assert 'makeElement("summary", "candidate-summary")' in render
    assert 'makeElement("fieldset", "candidate-editor")' in render
    assert '"sr-only"' in render
    assert "card.open = true" in render
    assert "card.open = false" not in render
    assert 'event.stopPropagation()' in render


def test_review_apply_updates_editor_only_and_never_executes() -> None:
    apply_block = function_block(javascript_source(), "applyReviewedCandidates")

    assert re.search(r"\bpayload\s*=", apply_block)
    assert "syncEditor()" in apply_block
    assert "renderCaseOverview()" in apply_block
    assert "resetResults()" in apply_block
    assert "normalizedBaseUrl(current.base_url)" in apply_block
    assert "existingIds" in apply_block
    assert "collision" in apply_block
    assert "fetch(" not in apply_block
    assert "runTests(" not in apply_block
    assert "/api/v1/runs" not in apply_block


def test_candidate_conversion_accepts_safe_edits_and_rejects_invalid_values(
) -> None:
    javascript = javascript_source()
    definitions = "\n".join(
        [
            "function parseCandidateJson(source, label, candidate) {"
            + function_block(javascript, "parseCandidateJson"),
            "function candidateToTestCase(candidate) {"
            + function_block(javascript, "candidateToTestCase"),
            "function normalizedBaseUrl(value) {"
            + function_block(javascript, "normalizedBaseUrl"),
        ]
    )
    candidate = {
        "caseId": "candidate_1",
        "name": "Edited candidate",
        "method": "POST",
        "path": "/items/7",
        "queryText": '{"limit": 1}',
        "bodyText": '{"name": "synthetic"}',
        "expectedStatus": "422",
    }
    program = (
        '"use strict";\n'
        + definitions
        + "\n"
        + f"const candidate = {json.dumps(candidate)};\n"
        + """
function attempt(changes) {
  try {
    const testCase = candidateToTestCase({...candidate, ...changes});
    return {accepted: true, testCase};
  } catch (error) {
    return {accepted: false, message: error.message};
  }
}
const result = {
  valid: attempt({}),
  invalidPath: attempt({path: "https://evil.example.test/items"}),
  invalidQuery: attempt({queryText: "[]"}),
  invalidBody: attempt({bodyText: "{bad"}),
  invalidStatus: attempt({expectedStatus: "700"}),
  normalized: normalizedBaseUrl("https://api.example.test/"),
};
process.stdout.write(JSON.stringify(result));
"""
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
    result = json.loads(completed.stdout)
    assert result["valid"]["accepted"] is True
    test_case = result["valid"]["testCase"]
    assert test_case["headers"] == {}
    assert test_case["depends_on"] == []
    assert test_case["extract"] == []
    assert test_case["assertions"] == [
        {"type": "status_code", "expected": 422, "path": None}
    ]
    for key in ("invalidPath", "invalidQuery", "invalidBody", "invalidStatus"):
        assert result[key]["accepted"] is False
    assert result["normalized"] == "https://api.example.test"


def test_openapi_input_changes_invalidate_both_candidate_sources() -> None:
    javascript = javascript_source()
    invalid_openapi = function_block(javascript, "invalidateGeneratedOpenApi")
    invalid_ai = function_block(javascript, "invalidateGeneratedAi")

    assert 'removeReviewSource("openapi")' in invalid_openapi
    assert 'removeReviewSource("ai")' in invalid_ai


def test_candidate_review_exposes_manual_candidate_evaluation_and_feedback_dialog() -> None:
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
    for output_id in ("ai-feedback-score", "ai-feedback-issues"):
        assert re.search(rf'\bid=["\']{output_id}["\']', html)


def test_candidate_evaluation_failure_does_not_clear_registered_candidates() -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateAiCases")
    evaluator = function_block(javascript, "evaluateCandidates")
    assert 'registerReviewSource("ai", result.run, result.insights)' in generator
    assert re.search(r"evaluateCandidates\s*\(.*manual\s*:\s*false", generator, re.DOTALL)
    assert re.search(r"catch\s*\([^)]*\)", evaluator)
    assert "setCandidateEvaluationStatus(" in evaluator
    assert "removeReviewSource" not in evaluator

    evaluator_definition = (
        "async function evaluateCandidates(source, { manual = false } = {}) {"
        + evaluator
    )
    program = (
        '"use strict";\n'
        + evaluator_definition
        + "\n"
        + "let reviewCandidates = [{caseId: 'candidate_1'}];\n"
        + "const statuses = [];\n"
        + "const loadingStates = [];\n"
        + "let removeReviewSourceCalled = false;\n"
        + "let runIsLoading = false;\n"
        + "let openApiIsLoading = false;\n"
        + "let candidateEvaluationIsLoading = false;\n"
        + "let candidateEvaluationAbortController = null;\n"
        + "let candidateEvaluationRequestSequence = 0;\n"
        + "let candidateEvaluationResult = null;\n"
        + "const elements = {aiObjective: {value: ''}, openApiEditor: {}};\n"
        + "class AbortController { constructor() { this.signal = {}; } abort() {} }\n"
        + "function evaluationCaseIds(run) { return new Set(run.cases.map((testCase) => testCase.id)); }\n"
        + "function readCurrentOpenApiDocument() { return {openapi: '3.0.0', info: {title: 'synthetic'}, paths: {}}; }\n"
        + "function setCandidateEvaluationStatus(message, status) { statuses.push({message, status}); }\n"
        + "function setCandidateEvaluationLoading(value) { loadingStates.push(value); }\n"
        + "function describeApiError() { return 'synthetic provider unavailable'; }\n"
        + "function openAiFeedback() {}\n"
        + "function removeReviewSource() { removeReviewSourceCalled = true; }\n"
        + "globalThis.fetch = async () => ({ok: false, status: 502, json: async () => ({detail: 'synthetic provider unavailable'})});\n"
        + "const generatedSource = {run: {cases: [{id: 'generated_case'}]}, insights: []};\n"
        + "(async () => {\n"
        + "  await evaluateCandidates(generatedSource, {manual: false});\n"
        + "  process.stdout.write(JSON.stringify({candidateCount: reviewCandidates.length, statuses, removed: removeReviewSourceCalled}));\n"
        + "})();\n"
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
    result = json.loads(completed.stdout)
    assert result["candidateCount"] == 1
    assert result["removed"] is False
    assert any(status["status"] == "error" for status in result["statuses"])


def test_draft_input_handlers_only_mark_feedback_stale_without_fetching() -> None:
    javascript = javascript_source()
    draft = function_block(javascript, "evaluateCurrentDraft")
    assert 'fetch("/api/v1/ai/drafts/evaluate"' in draft
    assert 'credentials: "same-origin"' in draft
    assert "validateAiEvaluationResponse(" in draft
    assert "/api/v1/runs" not in draft
    assert "runTests(" not in draft
    for element_name in ("baseUrl", "jsonEditor"):
        match = re.search(
            rf"(?ms)^elements\.{element_name}\.addEventListener\(\s*[\"']input[\"'].*?"
            r"(?=^elements\.\w+\.addEventListener|\Z)",
            javascript,
        )
        assert match is not None, element_name
        input_handler = match.group(0)
        assert "markDraftEvaluationStale(" in input_handler
        assert "fetch(" not in input_handler
        assert "evaluateCurrentDraft(" not in input_handler
