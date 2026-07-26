from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
repository_root = Path(__file__).resolve().parent.parent
frontend_directory = repository_root / "frontend"


def html_source() -> str:
    return client.get("/").text


def javascript_source() -> str:
    return (frontend_directory / "app.js").read_text(encoding="utf-8")


def function_block(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{"
        rf"(?P<body>.*?)(?=^(?:async\s+)?function\s+\w+\s*\(|"
        rf"^elements\.\w+\.addEventListener|\Z)",
        source,
    )
    assert match is not None, f"missing JavaScript function: {name}"
    return match.group("body")


def test_openapi_section_exposes_all_labeled_accessible_controls() -> None:
    html = html_source()

    assert re.search(
        r"""<section\b[^>]*\bid=["']openapi-generator["'][^>]*"""
        r"""\baria-labelledby=["'][^"']+["']""",
        html,
    )
    controls = {
        "openapi-editor": "textarea",
        "openapi-file": "input",
        "openapi-base-url": "input",
        "openapi-max-cases": "input",
    }
    for control_id, tag in controls.items():
        assert re.search(
            rf"""<label\b[^>]*\bfor=["']{control_id}["']""",
            html,
        )
        assert re.search(
            rf"""<{tag}\b[^>]*\bid=["']{control_id}["']""",
            html,
        )

    assert re.search(
        r"""<input\b[^>]*\bid=["']openapi-file["'][^>]*"""
        r"""\baccept=["'][^"']*(?:\.json|application/json)[^"']*["']""",
        html,
    )
    assert re.search(
        r"""<input\b[^>]*\bid=["']openapi-max-cases["'][^>]*"""
        r"""\bmin=["']?1["']?[^>]*\bmax=["']?50["']?[^>]*"""
        r"""\bvalue=["']?20["']?""",
        html,
    )
    for button_id in (
        "load-openapi-demo",
        "generate-openapi",
        "apply-generated-run",
    ):
        assert re.search(
            rf"""<button\b[^>]*\bid=["']{button_id}["'][^>]*"""
            r"""\btype=["']button["']""",
            html,
        )


def test_openapi_output_has_live_status_summary_and_warning_dom() -> None:
    html = html_source()

    assert re.search(
        r"""<[^>]+\bid=["']openapi-status["'][^>]*"""
        r"""\brole=["']status["'][^>]*"""
        r"""\baria-live=["']polite["']""",
        html,
    )
    for output_id in (
        "openapi-generated-count",
        "openapi-skipped-count",
        "openapi-warnings",
    ):
        assert re.search(rf"""\bid=["']{output_id}["']""", html)

    apply_button = re.search(
        r"""<button\b[^>]*\bid=["']apply-generated-run["'][^>]*>""",
        html,
    )
    assert apply_button is not None
    assert re.search(r"\bdisabled\b", apply_button.group(0))
    assert any(
        guidance in html
        for guidance in (
            "不要放入真实密钥",
            "不得包含真实密钥",
            "请勿放入真实密钥",
        )
    )


def test_file_size_is_checked_before_any_file_text_read() -> None:
    javascript = javascript_source()
    handler = function_block(javascript, "handleOpenApiFile")

    assert re.search(
        r"const\s+MAX_OPENAPI_FILE_BYTES\s*=\s*1_?048_?576",
        javascript,
    )
    size_check = re.search(
        r"""file\.size\s*>\s*(?:MAX_OPENAPI_FILE_BYTES|"""
        r"""1_?048_?576|1024\s*\*\s*1024)""",
        handler,
    )
    type_check = re.search(
        r"""(?:file\.type|file\.name)""",
        handler,
    )
    text_read = re.search(r"(?:await\s+)?file\.text\s*\(\s*\)", handler)
    assert size_check is not None
    assert type_check is not None
    assert text_read is not None
    assert type_check.start() < text_read.start()
    assert size_check.start() < text_read.start()


def test_file_type_gate_has_the_frozen_mime_and_extension_truth_table() -> None:
    javascript = javascript_source()
    handler = function_block(javascript, "handleOpenApiFile")
    text_read = handler.index("file.text")

    extension_check = re.search(
        r"""(?:const\s+)?hasJsonExtension\s*="""
        r"""\s*fileName\.endsWith\s*\(\s*["']\.json["']\s*\)""",
        handler,
    )
    mime_check = re.search(
        r"""(?:const\s+)?hasJsonMime\s*="""
        r"""\s*fileType\s*===\s*["']application/json["']""",
        handler,
    )
    rejection = re.search(
        r"""if\s*\(\s*!hasJsonExtension\s*&&\s*!hasJsonMime\s*\)""",
        handler,
    )

    assert extension_check is not None
    assert mime_check is not None
    assert rejection is not None
    assert extension_check.start() < text_read
    assert mime_check.start() < text_read
    assert rejection.start() < text_read

    accepts_file = lambda name, mime: (
        name.lower().endswith(".json") or mime.lower() == "application/json"
    )
    assert accepts_file("synthetic.txt", "") is False
    assert accepts_file("synthetic.json", "") is True
    assert accepts_file("synthetic.txt", "application/json") is True


def test_successful_file_read_also_clears_the_file_input() -> None:
    javascript = javascript_source()
    handler = function_block(javascript, "handleOpenApiFile")
    text_read = handler.index("file.text")
    finally_block = re.search(
        r"finally\s*\{(?P<body>.*?)\}",
        handler,
        re.DOTALL,
    )

    assert finally_block is not None
    assert finally_block.start() > text_read
    assert re.search(
        r"""elements\.openApiFile\.value\s*=\s*["']["']""",
        finally_block.group("body"),
    )


def test_openapi_input_requires_json_object_and_bounded_integer_max_cases(
) -> None:
    javascript = javascript_source()
    reader = function_block(javascript, "readOpenApiRequest")

    assert "JSON.parse" in reader
    assert re.search(
        r"""typeof\s+\w+\s*!==\s*["']object["']""",
        reader,
    )
    assert re.search(r"Array\.isArray\s*\(", reader)
    assert re.search(r"Number\.isInteger\s*\(", reader)
    assert re.search(r"(?:<\s*1|>=\s*1)", reader)
    assert re.search(r"(?:>\s*50|<=\s*50)", reader)
    assert "max_cases" in reader


def test_generation_posts_only_to_same_origin_with_credentials() -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateOpenApiCases")

    assert re.search(
        r"""fetch\s*\(\s*["']/api/v1/openapi/generate["']\s*,\s*\{"""
        r"""(?:(?!\}\s*\)).)*method\s*:\s*["']POST["']"""
        r"""(?:(?!\}\s*\)).)*credentials\s*:\s*["']same-origin["']""",
        generator,
        re.DOTALL,
    )
    assert "/api/v1/runs" not in generator
    assert not re.search(
        r"""fetch\s*\(\s*(?:["']https?://|`https?://|["']//|`//)""",
        generator,
    )


def test_success_response_is_validated_before_it_can_be_applied() -> None:
    javascript = javascript_source()
    validator = function_block(javascript, "validateGeneratedResponse")
    generator = function_block(javascript, "generateOpenApiCases")

    assert re.search(r"\.run\b", validator)
    assert re.search(r"Array\.isArray\s*\([^)]*\.cases\s*\)", validator)
    assert re.search(r"generated_count", validator)
    assert re.search(r"skipped_count", validator)
    assert re.search(r"warnings", validator)
    assert len(re.findall(r"Number\.isInteger\s*\(", validator)) >= 2
    assert re.search(r"generated_count\s*<\s*0", validator)
    assert re.search(r"skipped_count\s*<\s*0", validator)
    assert re.search(
        r"generated_count\s*!==\s*(?:[^;\n]*\.)?cases\.length",
        validator,
    )
    assert re.search(r"Array\.isArray\s*\([^)]*warnings[^)]*\)", validator)
    for field in ("location", "code", "message"):
        assert re.search(
            rf"""typeof\s+[^;\n]*warning\.{field}\s*(?:===|!==)"""
            rf"""\s*["']string["']""",
            validator,
        )
    validation_call = re.search(r"validateGeneratedResponse\s*\(", generator)
    response_assignment = re.search(
        r"(?:generatedOpenApi|generatedResponse|generatedRun)\s*=",
        generator,
    )
    assert validation_call is not None
    assert response_assignment is not None
    assert validation_call.start() < response_assignment.start()


def test_warnings_and_api_values_use_safe_text_dom_rendering() -> None:
    javascript = javascript_source()
    renderer = function_block(javascript, "renderOpenApiWarnings")

    assert "safeWarning.message" in renderer
    assert "safeWarning.code" in renderer
    assert "safeWarning.location" in renderer
    assert "makeElement(" in renderer or "textContent" in renderer
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "console.",
    ):
        assert forbidden not in javascript


def test_apply_only_updates_editor_state_and_never_executes_tests() -> None:
    javascript = javascript_source()
    apply_block = function_block(javascript, "applyGeneratedRun")

    assert re.search(r"\bpayload\s*=", apply_block)
    assert "baseUrl.value" in apply_block
    assert "syncEditor()" in apply_block
    assert "renderCaseOverview()" in apply_block
    assert "resetResults()" in apply_block
    assert "fetch(" not in apply_block
    assert "runTests(" not in apply_block
    assert "/api/v1/runs" not in apply_block


def test_openapi_loading_disables_mutating_actions_and_recovers() -> None:
    javascript = javascript_source()
    loading = function_block(javascript, "setOpenApiLoading")
    controls = function_block(javascript, "syncMutatingControlState")
    generator = function_block(javascript, "generateOpenApiCases")

    for element_name in (
        "openApiEditor",
        "openApiFile",
        "openApiBaseUrl",
        "openApiMaxCases",
        "loadOpenApiDemo",
        "generateOpenApi",
        "applyGeneratedRun",
        "runTests",
    ):
        assert re.search(
            rf"elements\.{element_name}\.disabled\s*=",
            controls,
        )
    assert "syncMutatingControlState()" in loading
    assert re.search(r"""aria-busy["']?\s*,""", loading)
    assert re.search(
        r"setOpenApiLoading\s*\(\s*true\s*\)"
        r"(?:(?!finally).)*finally\s*\{"
        r"(?:(?!\}).)*setOpenApiLoading\s*\(\s*false\s*\)",
        generator,
        re.DOTALL,
    )

    run_loading = function_block(javascript, "setLoading")
    assert "syncMutatingControlState()" in run_loading


def test_stale_generation_responses_cannot_replace_newer_input() -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateOpenApiCases")
    invalidator = function_block(javascript, "invalidateGeneratedOpenApi")

    sequence_names = re.findall(
        r"\b([A-Za-z_$][\w$]*(?:Request|Generation|Sequence|Version|Epoch)"
        r"[A-Za-z_$\d]*)\b",
        generator,
        flags=re.IGNORECASE,
    )
    assert sequence_names, "generation must capture a stale-response token"
    assert re.search(
        r"(?:!==|!=)\s*[A-Za-z_$][\w$]*",
        generator,
    )
    assert re.search(r"(?:return|throw)\b", generator)
    assert re.search(
        r"(?:generatedOpenApi|generatedResponse|generatedRun)\s*=\s*null",
        invalidator,
    )


def test_input_changes_and_generation_failures_clear_the_apply_candidate(
) -> None:
    javascript = javascript_source()
    generator = function_block(javascript, "generateOpenApiCases")

    catch_block = re.search(
        r"catch\s*\([^)]*\)\s*\{(?P<body>.*?)(?=\}\s*finally)",
        generator,
        re.DOTALL,
    )
    assert catch_block is not None
    assert "invalidateGeneratedOpenApi" in catch_block.group("body")

    binding_match = re.search(
        r"\[\s*elements\.openApiEditor,\s*"
        r"elements\.openApiBaseUrl,\s*"
        r"elements\.openApiMaxCases,\s*\]",
        javascript,
    )
    assert binding_match is not None
    binding_start = binding_match.start()
    binding_end = javascript.index("restoreDemo();", binding_start)
    binding_source = javascript[binding_start:binding_end]
    for element_name in (
        "openApiEditor",
        "openApiBaseUrl",
        "openApiMaxCases",
    ):
        assert f"elements.{element_name}" in binding_source
    assert re.search(
        r"""addEventListener\s*\(\s*["']input["']""",
        binding_source,
    )
    assert "invalidateGeneratedOpenApi" in binding_source

    file_handler = function_block(javascript, "handleOpenApiFile")
    assert "invalidateGeneratedOpenApi" in file_handler


def test_openapi_console_exposes_all_frozen_error_states() -> None:
    javascript = javascript_source()

    expected_fragments = (
        "JSON",
        "对象",
        "1 MiB",
        "响应",
        "生成",
        "失败",
    )
    for fragment in expected_fragments:
        assert fragment in javascript
    assert re.search(r"1\s*到\s*50", javascript)

    assert "describeApiError" in javascript
    assert re.search(r"catch\s*\(", javascript)
    assert re.search(r"setOpenApiStatus\s*\(", javascript)


def test_malicious_success_shapes_never_enable_or_assign_apply_candidate(
) -> None:
    javascript = javascript_source()
    validator_definition = (
        "function validateGeneratedResponse(data) {"
        + function_block(javascript, "validateGeneratedResponse")
    )
    valid = {
        "generated_count": 1,
        "skipped_count": 0,
        "warnings": [],
        "run": {
            "base_url": "https://api.example.test/v1",
            "variables": {},
            "secret_variables": [],
            "cases": [
                {
                    "id": "safe_case",
                    "name": "Synthetic safe case",
                    "method": "GET",
                    "path": "/safe",
                    "headers": {},
                    "query": {},
                    "json_body": None,
                    "assertions": [
                        {
                            "type": "status_code",
                            "expected": 200,
                            "path": None,
                        }
                    ],
                    "depends_on": [],
                    "extract": [],
                }
            ],
        },
    }

    malicious: dict[str, dict] = {}

    def variant(name: str) -> dict:
        value = deepcopy(valid)
        malicious[name] = value
        return value

    variant("nonempty_variables")["run"]["variables"] = {
        "synthetic_token": "must-not-apply"
    }
    variant("nonempty_secret_variables")["run"]["secret_variables"] = [
        "synthetic_token"
    ]
    variant("authorization_header")["run"]["cases"][0]["headers"] = {
        "Authorization": "Bearer synthetic"
    }
    variant("cookie_header")["run"]["cases"][0]["headers"] = {
        "Cookie": "synthetic_session=value"
    }
    for name, base_url in {
        "relative_base_url": "/relative",
        "credential_base_url": "https://user:password@example.test",
        "fragment_base_url": "https://example.test/#fragment",
        "brace_base_url": "https://{tenant}.example.test",
        "invalid_scheme_base_url": "javascript:alert(1)",
    }.items():
        variant(name)["run"]["base_url"] = base_url
    variant("network_path")["run"]["cases"][0]["path"] = "//evil.example.test"
    variant("duplicate_status_assertions")["run"]["cases"][0][
        "assertions"
    ].append(
        {
            "type": "status_code",
            "expected": 201,
            "path": None,
        }
    )
    variant("non_2xx_status")["run"]["cases"][0]["assertions"][0][
        "expected"
    ] = 401
    variant("nonempty_depends_on")["run"]["cases"][0]["depends_on"] = [
        "earlier_case"
    ]
    variant("nonempty_extract")["run"]["cases"][0]["extract"] = [
        {
            "name": "synthetic_token",
            "path": "data.token",
            "secret": True,
        }
    ]

    node_program = (
        '"use strict";\n'
        + validator_definition
        + "\n"
        + f"const valid = {json.dumps(valid, ensure_ascii=False)};\n"
        + f"const malicious = {json.dumps(malicious, ensure_ascii=False)};\n"
        + """
function attempt(candidate) {
  let generatedRun = null;
  let applyDisabled = true;
  try {
    const result = validateGeneratedResponse(candidate);
    generatedRun = result.run;
    applyDisabled = generatedRun === null;
    return {accepted: true, assigned: generatedRun !== null, applyDisabled};
  } catch (_error) {
    return {accepted: false, assigned: generatedRun !== null, applyDisabled};
  }
}
const result = {
  valid: attempt(valid),
  malicious: Object.fromEntries(
    Object.entries(malicious).map(([name, value]) => [name, attempt(value)]),
  ),
};
process.stdout.write(JSON.stringify(result));
"""
    )
    completed = subprocess.run(
        ["node", "-"],
        input=node_program,
        text=True,
        capture_output=True,
        check=False,
        cwd=repository_root,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["valid"] == {
        "accepted": True,
        "assigned": True,
        "applyDisabled": False,
    }
    assert result["malicious"]
    for name, outcome in result["malicious"].items():
        assert outcome == {
            "accepted": False,
            "assigned": False,
            "applyDisabled": True,
        }, name
