from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.schemas import (
    AiCandidateEvaluationRequest,
    AiCaseInsight,
    AiDraftEvaluationRequest,
    AiEvaluationIssue,
    AiEvaluationResponse,
    AiEvaluationRole,
    AiEvaluationSeverity,
    AiGenerateRequest,
    AiGenerateResponse,
    AiProviderStatus,
    AssertionRule,
    AssertionType,
    HttpMethod,
    OpenApiGenerateRequest,
    OpenApiGenerationWarning,
    TestCase,
    TestRunRequest,
)
from app.services.openapi_generator import (
    OpenApiGenerationError,
    generate_openapi_cases,
)

MAX_DOCUMENT_BYTES = 1_048_576
MAX_PATHS = 200
MAX_OPERATIONS = 50
MAX_PROMPT_BYTES = 65_536
MAX_SCHEMA_DEPTH = 5
MAX_CASE_BYTES = 65_536
MAX_CANDIDATE_EVALUATION_CASES = 10
SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete")
SAFE_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:authorization|api[-_]?key|token|secret|password|cookie|session)",
    re.IGNORECASE,
)
CREDENTIAL_VALUE_PATTERN = re.compile(
    r"(?:bearer\s+\S+|(?:api[-_]?key|token|secret|password|authorization|cookie)\s*(?:=|:)\s*\S+)",
    re.IGNORECASE,
)
ABSOLUTE_URL_PATTERN = re.compile(
    r"\b(?:https?|wss?)://[^\s<>'\"`]+",
    re.IGNORECASE,
)
PROVIDER_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?:authorization|api[-_ ]?key|token|secret|password|cookie|session|bearer)\b",
    re.IGNORECASE,
)


class AiAssistantError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class CandidateCategory(StrEnum):
    BOUNDARY = "boundary"
    NEGATIVE = "negative"
    ROBUSTNESS = "robustness"


class CandidateQueryParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    value: str | int | float | bool | None


class ProviderCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=500)
    category: CandidateCategory
    method: HttpMethod
    path: str = Field(min_length=1, max_length=2048)
    query: list[CandidateQueryParameter]
    json_body_json: str | None
    expected_status: int = Field(ge=100, le=599)


class ProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ProviderCandidate] = Field(min_length=1, max_length=10)


class ProviderEvaluation(AiEvaluationResponse):
    """The untrusted, structured evaluation returned by an AI provider."""


class AiProvider(Protocol):
    name: str
    model: str | None

    def generate(
        self,
        *,
        outline: dict[str, Any],
        objective: str,
        max_cases: int,
    ) -> ProviderOutput: ...

    def evaluate(
        self,
        *,
        role: AiEvaluationRole,
        evaluation_input: dict[str, Any],
        objective: str,
    ) -> ProviderEvaluation: ...


class MockAiProvider:
    name = "mock"
    model = None

    def generate(
        self,
        *,
        outline: dict[str, Any],
        objective: str,
        max_cases: int,
    ) -> ProviderOutput:
        del objective
        candidates: list[ProviderCandidate] = []
        for index, operation in enumerate(
            outline["operations"][:max_cases],
            start=1,
        ):
            parameters = operation["parameters"]
            path = operation["path"]
            for parameter in parameters:
                if parameter["in"] != "path":
                    continue
                sample = _safe_sample(parameter.get("schema"))
                path = path.replace(
                    "{" + parameter["name"] + "}",
                    str(sample),
                )
            query = [
                CandidateQueryParameter(
                    name=parameter["name"],
                    value=_safe_sample(parameter.get("schema")),
                )
                for parameter in parameters
                if parameter["in"] == "query" and parameter["required"]
            ]
            body = _safe_sample(operation.get("request_body"))
            if operation.get("request_body") is None:
                body = None
            category = CandidateCategory.ROBUSTNESS
            rationale = "Mock Provider 演示候选用例生成链路；请人工检查后再运行。"
            expected_status = _preferred_success_status(
                operation["response_statuses"]
            )

            if query:
                query[0].value = _boundary_value(query[0].value)
                category = CandidateCategory.BOUNDARY
                rationale = (
                    f"将必填查询参数 {query[0].name} 调整为边界值，"
                    "用于演示参数校验候选场景。"
                )
                expected_status = 400
            elif isinstance(body, dict) and body:
                first_key = next(iter(body))
                body = dict(body)
                body[first_key] = None
                category = CandidateCategory.NEGATIVE
                rationale = (
                    f"将 JSON 字段 {first_key} 置空，"
                    "用于演示必填字段异常候选场景。"
                )
                expected_status = 400

            candidates.append(
                ProviderCandidate(
                    id=f"ai_mock_{index}",
                    name=(
                        "AI 候选："
                        + (
                            operation.get("summary")
                            or operation.get("operation_id")
                            or f"{operation['method']} {operation['path']}"
                        )
                    )[:120],
                    rationale=rationale,
                    category=category,
                    method=operation["method"],
                    path=path,
                    query=query,
                    json_body_json=(
                        json.dumps(body, ensure_ascii=False)
                        if body is not None
                        else None
                    ),
                    expected_status=expected_status,
                )
            )
        return ProviderOutput(candidates=candidates)

    def evaluate(
        self,
        *,
        role: AiEvaluationRole,
        evaluation_input: dict[str, Any],
        objective: str,
    ) -> ProviderEvaluation:
        del objective
        cases = evaluation_input.get("cases", [])
        if not isinstance(cases, list):
            raise _invalid_output()

        if role is AiEvaluationRole.CANDIDATE_EVALUATOR:
            insights = evaluation_input.get("insights", [])
            categories = {
                item.get("category")
                for item in insights
                if isinstance(item, dict) and isinstance(item.get("category"), str)
            }
            missing_categories = sorted(
                set(CandidateCategory._value2member_map_) - categories
            )
            issues = [
                AiEvaluationIssue(
                    case_id=None,
                    severity=AiEvaluationSeverity.WARNING,
                    title="Candidate category coverage is incomplete",
                    detail="The candidate set does not cover every advisory category.",
                    suggestion="Add a human-reviewed case for the missing category.",
                )
            ] if missing_categories else []
            return ProviderEvaluation(
                role=role,
                provider=self.name,
                model=self.model,
                score=max(0, 90 - 10 * len(missing_categories)),
                summary="Mock candidate evaluation completed from the sanitized case summary.",
                strengths=["Candidate evaluation uses only relative-path case metadata."],
                issues=issues,
                recommendations=["Review all candidates before adding them to a draft."],
                evaluated_case_count=len(cases),
                requires_human_review=True,
            )

        executable = sum(
            1
            for case in cases
            if isinstance(case, dict) and case.get("assertions")
        )
        issues = []
        if executable != len(cases):
            issues.append(
                AiEvaluationIssue(
                    case_id=None,
                    severity=AiEvaluationSeverity.WARNING,
                    title="Some draft cases have no assertion metadata",
                    detail="A draft case without assertions cannot provide a useful result.",
                    suggestion="Add at least one human-reviewed assertion to each case.",
                )
            )
        return ProviderEvaluation(
            role=role,
            provider=self.name,
            model=self.model,
            score=85 if executable == len(cases) else 65,
            summary="Mock draft evaluation completed from the sanitized draft summary.",
            strengths=["Draft evaluation checks assertions and dependency metadata."],
            issues=issues,
            recommendations=["Review the draft before manually running it."],
            evaluated_case_count=len(cases),
            requires_human_review=True,
        )


class OpenAiProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def generate(
        self,
        *,
        outline: dict[str, Any],
        objective: str,
        max_cases: int,
    ) -> ProviderOutput:
        prompt = _generation_prompt(
            objective=objective,
            outline=outline,
            max_cases=max_cases,
        )
        _validate_generation_prompt_size(prompt)

        body = {
            "model": self.model,
            "store": False,
            "instructions": (
                "You are a defensive API test case designer. Return only candidate "
                "boundary, negative, or robustness tests for the supplied API "
                "structure. Never invent credentials, headers, absolute URLs, "
                "dependencies, extraction rules, or destructive production actions. "
                "Keep paths relative and use only listed operations and query names. "
                "The result is a draft that requires human review."
            ),
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "api_test_candidates",
                    "strict": True,
                    "schema": ProviderOutput.model_json_schema(),
                }
            },
            "max_output_tokens": 5000,
        }
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AiAssistantError(
                "AI_PROVIDER_UNAVAILABLE",
                "AI provider request failed",
                status_code=502,
            ) from None

        output_text = _response_output_text(payload)
        try:
            return ProviderOutput.model_validate_json(output_text)
        except (ValidationError, ValueError):
            raise AiAssistantError(
                "AI_PROVIDER_INVALID_OUTPUT",
                "AI provider returned an invalid structured response",
                status_code=502,
            ) from None

    def evaluate(
        self,
        *,
        role: AiEvaluationRole,
        evaluation_input: dict[str, Any],
        objective: str,
    ) -> ProviderEvaluation:
        prompt = _evaluation_prompt(
            objective=objective,
            evaluation_input=evaluation_input,
        )
        _validate_evaluation_prompt_size(prompt)
        body = {
            "model": self.model,
            "store": False,
            "instructions": _evaluation_instructions(role),
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "api_test_evaluation",
                    "strict": True,
                    "schema": ProviderEvaluation.model_json_schema(),
                }
            },
        }
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AiAssistantError(
                "AI_PROVIDER_UNAVAILABLE",
                "AI provider request failed",
                status_code=502,
            ) from None

        try:
            return ProviderEvaluation.model_validate_json(_response_output_text(payload))
        except (AiAssistantError, ValidationError, ValueError):
            raise AiAssistantError(
                "AI_PROVIDER_INVALID_OUTPUT",
                "AI provider returned an invalid structured response",
                status_code=502,
            ) from None


class AiAssistantService:
    def __init__(
        self,
        settings: Settings,
        *,
        provider: AiProvider | None = None,
    ) -> None:
        self._settings = settings
        if (
            provider is None
            and settings.ai_provider == "openai"
            and settings.openai_api_key is None
        ):
            self._provider = None
        else:
            self._provider = provider or _provider_from_settings(settings)

    def status(self) -> AiProviderStatus:
        configured = self._settings.ai_provider == "mock" or bool(
            self._settings.openai_api_key
        )
        return AiProviderStatus(
            provider=self._settings.ai_provider,
            configured=configured,
            model=(
                getattr(self._provider, "model", None)
                if self._provider is not None
                else self._settings.openai_model
            ),
            network_access=self._settings.ai_provider == "openai",
        )

    def generate(self, request: AiGenerateRequest) -> AiGenerateResponse:
        baseline = _baseline_run(request)
        outline = build_safe_outline(request.document)
        _validate_generation_prompt_size(
            _generation_prompt(
                objective=request.objective,
                outline=outline,
                max_cases=request.max_cases,
            )
        )
        if self._provider is None:
            raise AiAssistantError(
                "AI_PROVIDER_NOT_CONFIGURED",
                "OpenAI provider requires OPENAI_API_KEY",
                status_code=503,
            )
        provider_output = self._provider.generate(
            outline=outline,
            objective=_sanitize_provider_text(request.objective, 500) or "",
            max_cases=request.max_cases,
        )
        cases, insights, warnings = _validate_candidates(
            provider_output,
            outline=outline,
            max_cases=request.max_cases,
        )
        return AiGenerateResponse(
            provider=self._provider.name,
            model=self._provider.model,
            generated_count=len(cases),
            warnings=warnings,
            insights=insights,
            run=TestRunRequest(
                base_url=baseline.base_url,
                variables={},
                secret_variables=[],
                cases=cases,
            ),
        )

    def evaluate_candidates(
        self, request: AiCandidateEvaluationRequest
    ) -> AiEvaluationResponse:
        return self._evaluate(
            role=AiEvaluationRole.CANDIDATE_EVALUATOR,
            run=request.candidate_run,
            document=request.document,
            insights=request.insights,
            objective=request.objective,
            max_cases=MAX_CANDIDATE_EVALUATION_CASES,
        )

    def evaluate_draft(
        self, request: AiDraftEvaluationRequest
    ) -> AiEvaluationResponse:
        return self._evaluate(
            role=AiEvaluationRole.DRAFT_EVALUATOR,
            run=request.draft,
            document=request.document,
            insights=None,
            objective=request.objective,
            max_cases=50,
        )

    def _evaluate(
        self,
        *,
        role: AiEvaluationRole,
        run: TestRunRequest,
        document: dict[str, Any] | None,
        insights: list[AiCaseInsight] | None,
        objective: str,
        max_cases: int,
    ) -> AiEvaluationResponse:
        if len(run.cases) > max_cases:
            raise _invalid_evaluation_input("evaluation case count exceeds its limit")

        case_ids = _evaluation_case_ids(run)
        if insights is not None:
            _validate_evaluation_insights(insights, case_ids)
        evaluation_input = _build_evaluation_input(
            run=run,
            role=role,
            document=document,
            insights=insights,
        )
        _validate_evaluation_prompt_size(
            _evaluation_prompt(
                objective=_sanitize_provider_text(objective, 500) or "",
                evaluation_input=evaluation_input,
            )
        )
        if self._provider is None:
            raise AiAssistantError(
                "AI_PROVIDER_NOT_CONFIGURED",
                "OpenAI provider requires OPENAI_API_KEY",
                status_code=503,
            )
        evaluator = getattr(self._provider, "evaluate", None)
        if not callable(evaluator):
            raise _invalid_output()
        try:
            provider_evaluation = ProviderEvaluation.model_validate(
                evaluator(
                    role=role,
                    evaluation_input=evaluation_input,
                    objective=_sanitize_provider_text(objective, 500) or "",
                )
            )
        except AiAssistantError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise _invalid_output() from None
        except Exception:
            raise AiAssistantError(
                "AI_PROVIDER_UNAVAILABLE",
                "AI provider request failed",
                status_code=502,
            ) from None

        _validate_provider_evaluation(
            provider_evaluation,
            role=role,
            case_ids=case_ids,
            case_count=len(run.cases),
        )
        provider_name = getattr(self._provider, "name", None)
        provider_model = getattr(self._provider, "model", None)
        if not isinstance(provider_name, str) or not provider_name:
            raise _invalid_output()
        if provider_model is not None and not isinstance(provider_model, str):
            raise _invalid_output()
        return AiEvaluationResponse(
            **provider_evaluation.model_dump(
                exclude={"provider", "model", "requires_human_review"}
            ),
            provider=provider_name,
            model=provider_model,
            requires_human_review=True,
        )


def build_safe_outline(document: dict[str, Any]) -> dict[str, Any]:
    _validate_source_document_size(document)

    version = document.get("openapi")
    if not isinstance(version, str) or not re.fullmatch(r"3\.(?:0|1)\.\d+", version):
        raise AiAssistantError(
            "INVALID_AI_SOURCE_DOCUMENT",
            "only OpenAPI 3.0.x and 3.1.x documents are supported",
            status_code=422,
        )
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise AiAssistantError(
            "INVALID_AI_SOURCE_DOCUMENT",
            "OpenAPI paths must be an object",
            status_code=422,
        )
    if len(paths) > MAX_PATHS:
        raise AiAssistantError(
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
            "OpenAPI document has too many paths",
            status_code=422,
        )

    operations: list[dict[str, Any]] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(
            path_item, dict
        ):
            continue
        safe_path = _sanitize_provider_text(path, 2048, fallback=None)
        if safe_path is None:
            continue
        inherited = path_item.get("parameters", [])
        for method in SUPPORTED_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            parameters = _safe_parameters(inherited, operation.get("parameters", []))
            request_schema = _request_body_schema(operation.get("requestBody"))
            operations.append(
                {
                    "method": method.upper(),
                    "path": safe_path,
                    "operation_id": _safe_text(operation.get("operationId"), 120),
                    "summary": _safe_text(operation.get("summary"), 200),
                    "parameters": parameters,
                    "request_body": _safe_schema(request_schema, depth=0),
                    "response_statuses": _response_statuses(
                        operation.get("responses")
                    ),
                }
            )
            if len(operations) >= MAX_OPERATIONS:
                break
        if len(operations) >= MAX_OPERATIONS:
            break

    if not operations:
        raise AiAssistantError(
            "NO_AI_SOURCE_OPERATIONS",
            "OpenAPI document has no supported operations",
            status_code=422,
        )
    outline = {"openapi": version, "operations": operations}
    encoded = json.dumps(
        outline, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise AiAssistantError(
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
            "sanitized API structure exceeds the AI prompt limit",
            status_code=422,
        )
    return outline


def _validate_source_document_size(document: dict[str, Any]) -> None:
    try:
        serialized_size = len(
            json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        raise AiAssistantError(
            "INVALID_AI_SOURCE_DOCUMENT",
            "OpenAPI document must be JSON serializable",
            status_code=422,
        ) from None
    if serialized_size > MAX_DOCUMENT_BYTES:
        raise AiAssistantError(
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
            "OpenAPI document exceeds the 1 MiB limit",
            status_code=422,
        )


def _provider_from_settings(settings: Settings) -> AiProvider:
    if settings.ai_provider == "mock":
        return MockAiProvider()
    if settings.openai_api_key is None:
        raise AiAssistantError(
            "AI_PROVIDER_NOT_CONFIGURED",
            "OpenAI provider requires OPENAI_API_KEY",
            status_code=503,
        )
    return OpenAiProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model,
        timeout_seconds=settings.ai_request_timeout_seconds,
    )


def _build_evaluation_input(
    *,
    run: TestRunRequest,
    role: AiEvaluationRole,
    document: dict[str, Any] | None,
    insights: list[AiCaseInsight] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cases": _safe_evaluation_cases(run, role),
    }
    if document is not None:
        try:
            result["api_structure"] = build_safe_outline(document)
        except AiAssistantError as exc:
            if exc.code == "AI_SOURCE_DOCUMENT_TOO_LARGE":
                raise AiAssistantError(
                    "AI_EVALUATION_SOURCE_TOO_LARGE",
                    "evaluation source exceeds the allowed size",
                    status_code=422,
                ) from None
            raise _invalid_evaluation_input("evaluation source is invalid") from None
    if insights is not None:
        result["insights"] = [
            {
                "case_id": insight.case_id,
                "category": insight.category,
                "rationale": _sanitize_provider_text(insight.rationale, 500)
                or "[redacted-rationale]",
            }
            for insight in insights
        ]
    return result


def _safe_evaluation_cases(
    run: TestRunRequest,
    role: AiEvaluationRole,
) -> list[dict[str, Any]]:
    safe_cases: list[dict[str, Any]] = []
    for index, case in enumerate(run.cases, start=1):
        safe_query: list[dict[str, Any]] = []
        for name, value in case.query.items():
            safe_name = _sanitize_provider_text(name, 120, fallback=None)
            if safe_name is None:
                continue
            safe_query.append(
                {
                    "name": safe_name,
                    "value_shape": _safe_value_shape(value, depth=0),
                }
            )

        safe_extract: list[dict[str, str]] = []
        for rule in case.extract:
            if rule.secret:
                continue
            safe_name = _sanitize_provider_text(rule.name, 120, fallback=None)
            safe_path = _sanitize_provider_text(rule.path, 256, fallback=None)
            if safe_name is None or safe_path is None:
                continue
            safe_extract.append({"name": safe_name, "path": safe_path})

        safe_dependencies = [
            safe_dependency
            for dependency in case.depends_on
            if (
                safe_dependency := _sanitize_provider_text(
                    dependency, 64, fallback=None
                )
            )
            is not None
        ][:50]
        item: dict[str, Any] = {
            "id": case.id or f"case_{index}",
            "name": _sanitize_provider_text(case.name, 120)
            or "[unnamed-case]",
            "method": case.method.value,
            "path": _sanitize_provider_text(case.path, 2048)
            or "[redacted-path]",
            "query": safe_query[:50],
            "assertions": _safe_evaluation_assertions(case, role),
            "depends_on": safe_dependencies,
            "extract": safe_extract[:50],
        }
        if role is AiEvaluationRole.DRAFT_EVALUATOR:
            item["json_body_shape"] = _safe_value_shape(case.json_body, depth=0)
        safe_cases.append(item)
    return safe_cases


def _safe_evaluation_assertions(
    case: TestCase,
    role: AiEvaluationRole,
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for assertion in case.assertions[:50]:
        item: dict[str, Any] = {"type": assertion.type.value}
        safe_path = (
            _sanitize_provider_text(assertion.path, 256, fallback=None)
            if assertion.path
            else None
        )
        if safe_path is not None:
            item["path"] = safe_path
        if (
            role is AiEvaluationRole.CANDIDATE_EVALUATOR
            and assertion.type is AssertionType.STATUS_CODE
            and isinstance(assertion.expected, int)
        ):
            item["expected_status"] = assertion.expected
        elif role is AiEvaluationRole.DRAFT_EVALUATOR:
            item["expected_shape"] = _safe_value_shape(assertion.expected, depth=0)
        assertions.append(item)
    return assertions


def _safe_value_shape(value: Any, *, depth: int) -> dict[str, Any]:
    if depth >= MAX_SCHEMA_DEPTH:
        return {"type": "truncated"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {
            "type": "array",
            "items": [
                _safe_value_shape(item, depth=depth + 1) for item in value[:10]
            ],
        }
    if isinstance(value, dict):
        safe_properties: dict[str, dict[str, Any]] = {}
        for key, item in list(value.items())[:50]:
            safe_key = _sanitize_provider_text(str(key), 120, fallback=None)
            if safe_key is not None:
                safe_properties[safe_key] = _safe_value_shape(item, depth=depth + 1)
        return {
            "type": "object",
            "properties": safe_properties,
        }
    return {"type": "unknown"}


def _evaluation_prompt(*, objective: str, evaluation_input: dict[str, Any]) -> str:
    return json.dumps(
        {
            "objective": _sanitize_provider_text(objective, 500) or "",
            "evaluation": evaluation_input,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _generation_prompt(
    *,
    objective: str,
    outline: dict[str, Any],
    max_cases: int,
) -> str:
    return json.dumps(
        {
            "objective": _sanitize_provider_text(objective, 500) or "",
            "max_cases": max_cases,
            "api_structure": outline,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_generation_prompt_size(prompt: str) -> None:
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AiAssistantError(
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
            "sanitized API structure exceeds the AI prompt limit",
            status_code=422,
        )


def _validate_evaluation_prompt_size(prompt: str) -> None:
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AiAssistantError(
            "AI_EVALUATION_SOURCE_TOO_LARGE",
            "sanitized evaluation input exceeds the AI prompt limit",
            status_code=422,
        )


def _evaluation_instructions(role: AiEvaluationRole) -> str:
    if role is AiEvaluationRole.CANDIDATE_EVALUATOR:
        role_instruction = (
            "You are the candidate evaluator. Assess candidate coverage, category "
            "balance, and the supplied boundary or negative rationale."
        )
    else:
        role_instruction = (
            "You are the draft evaluator. Assess assertion coverage, dependency "
            "ordering, and whether each draft case is structurally executable."
        )
    return (
        f"{role_instruction} Use only the sanitized evaluation summary. Provide "
        "advisory feedback only: never modify cases, execute tests, persist data, "
        "request credentials, or include absolute URLs or raw secrets. Return only "
        "JSON matching the supplied schema. Human review is always required."
    )


def _evaluation_case_ids(run: TestRunRequest) -> set[str]:
    return {
        case.id or f"case_{index}"
        for index, case in enumerate(run.cases, start=1)
    }


def _validate_evaluation_insights(
    insights: list[AiCaseInsight], case_ids: set[str]
) -> None:
    if len(insights) > MAX_CANDIDATE_EVALUATION_CASES:
        raise _invalid_evaluation_input("too many candidate insights")
    seen: set[str] = set()
    allowed_categories = set(CandidateCategory._value2member_map_)
    for insight in insights:
        if (
            not isinstance(insight.case_id, str)
            or not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$", insight.case_id)
            or insight.case_id not in case_ids
            or insight.case_id in seen
            or insight.category not in allowed_categories
            or not isinstance(insight.rationale, str)
            or not 1 <= len(insight.rationale) <= 500
        ):
            raise _invalid_evaluation_input("candidate insights are invalid")
        seen.add(insight.case_id)


def _validate_provider_evaluation(
    evaluation: ProviderEvaluation,
    *,
    role: AiEvaluationRole,
    case_ids: set[str],
    case_count: int,
) -> None:
    if evaluation.role is not role or evaluation.evaluated_case_count != case_count:
        raise _invalid_output()
    for issue in evaluation.issues:
        if issue.case_id is not None and issue.case_id not in case_ids:
            raise _invalid_output()
    text_values = [
        evaluation.summary,
        *evaluation.strengths,
        *evaluation.recommendations,
        *(
            text
            for issue in evaluation.issues
            for text in (issue.title, issue.detail, issue.suggestion)
        ),
    ]
    if any(_unsafe_evaluation_text(value) for value in text_values):
        raise _invalid_output()


def _unsafe_evaluation_text(value: str) -> bool:
    return "://" in value or bool(CREDENTIAL_VALUE_PATTERN.search(value))


def _invalid_evaluation_input(message: str) -> AiAssistantError:
    return AiAssistantError(
        "INVALID_AI_EVALUATION_INPUT",
        message,
        status_code=422,
    )


def _baseline_run(request: AiGenerateRequest) -> TestRunRequest:
    _validate_source_document_size(request.document)
    try:
        generated = generate_openapi_cases(
            OpenApiGenerateRequest(
                document=request.document,
                base_url=request.base_url,
                max_cases=min(50, max(request.max_cases, 10)),
            )
        )
    except OpenApiGenerationError as exc:
        raise AiAssistantError(
            "INVALID_AI_SOURCE_DOCUMENT",
            "OpenAPI document cannot produce safe baseline cases",
            status_code=422,
        ) from exc
    return generated.run


def _validate_candidates(
    output: ProviderOutput,
    *,
    outline: dict[str, Any],
    max_cases: int,
) -> tuple[list[TestCase], list[AiCaseInsight], list[OpenApiGenerationWarning]]:
    allowed = {
        (operation["method"], operation["path"]): {
            parameter["name"]
            for parameter in operation["parameters"]
            if parameter["in"] == "query"
        }
        for operation in outline["operations"]
    }
    cases: list[TestCase] = []
    insights: list[AiCaseInsight] = []
    warnings: list[OpenApiGenerationWarning] = []
    seen_ids: set[str] = set()
    for candidate in output.candidates[:max_cases]:
        if candidate.id in seen_ids:
            raise _invalid_output()
        matching_template = _matching_template(
            candidate.method.value,
            candidate.path,
            allowed,
        )
        if matching_template is None:
            raise _invalid_output()
        allowed_query_names = allowed[(candidate.method.value, matching_template)]
        query_names = [item.name for item in candidate.query]
        if len(query_names) != len(set(query_names)) or not set(
            query_names
        ).issubset(allowed_query_names):
            raise _invalid_output()
        if candidate.path.startswith("//") or "://" in candidate.path:
            raise _invalid_output()

        body: Any | None = None
        if candidate.json_body_json is not None:
            if len(candidate.json_body_json.encode("utf-8")) > MAX_CASE_BYTES:
                raise _invalid_output()
            try:
                body = json.loads(candidate.json_body_json)
            except json.JSONDecodeError:
                raise _invalid_output() from None
            if _contains_sensitive_key(body):
                raise _invalid_output()

        case = TestCase(
            id=candidate.id,
            name=candidate.name,
            method=candidate.method,
            path=candidate.path,
            headers={},
            query={item.name: item.value for item in candidate.query},
            json_body=body,
            assertions=[
                AssertionRule(
                    type=AssertionType.STATUS_CODE,
                    expected=candidate.expected_status,
                )
            ],
            depends_on=[],
            extract=[],
        )
        if len(case.model_dump_json().encode("utf-8")) > MAX_CASE_BYTES:
            raise _invalid_output()
        cases.append(case)
        insights.append(
            AiCaseInsight(
                case_id=candidate.id,
                category=candidate.category.value,
                rationale=candidate.rationale,
            )
        )
        seen_ids.add(candidate.id)

    if not cases:
        raise _invalid_output()
    if len(output.candidates) > max_cases:
        warnings.append(
            OpenApiGenerationWarning(
                location="provider_output.candidates",
                code="AI_CASE_LIMIT_APPLIED",
                message="Provider candidates were truncated to the requested limit.",
            )
        )
    return cases, insights, warnings


def _safe_parameters(inherited: Any, operation_parameters: Any) -> list[dict[str, Any]]:
    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for collection in (inherited, operation_parameters):
        if not isinstance(collection, list):
            continue
        for parameter in collection:
            if not isinstance(parameter, dict) or "$ref" in parameter:
                continue
            name = parameter.get("name")
            location = parameter.get("in")
            safe_name = (
                _sanitize_provider_text(name, 120, fallback=None)
                if isinstance(name, str)
                else None
            )
            if (
                safe_name is None
                or location not in {"path", "query"}
            ):
                continue
            combined[(name, location)] = {
                "name": safe_name,
                "in": location,
                "required": bool(parameter.get("required")),
                "schema": _safe_schema(parameter.get("schema"), depth=0),
            }
    return list(combined.values())


def _safe_schema(value: Any, *, depth: int) -> dict[str, Any] | None:
    if not isinstance(value, dict) or depth >= MAX_SCHEMA_DEPTH:
        return None
    if "$ref" in value:
        return {"type": "referenced"}
    result: dict[str, Any] = {}
    schema_type = value.get("type")
    if isinstance(schema_type, str):
        result["type"] = (
            schema_type if schema_type in SAFE_SCHEMA_TYPES else "unknown"
        )
    for key in (
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    ):
        item = value.get(key)
        if isinstance(item, str):
            safe_item = _sanitize_provider_text(item, 256, fallback=None)
            if safe_item is not None:
                result[key] = safe_item
        elif isinstance(item, (int, float, bool)):
            result[key] = item
    if isinstance(value.get("enum"), list):
        result["enum_count"] = len(value["enum"])
    required = value.get("required")
    if isinstance(required, list):
        result["required"] = [
            safe_item
            for item in required
            if (
                isinstance(item, str)
                and (safe_item := _sanitize_provider_text(item, 120, fallback=None))
                is not None
            )
        ][:50]
    properties = value.get("properties")
    if isinstance(properties, dict):
        safe_properties = {}
        for name, child in list(properties.items())[:50]:
            safe_name = (
                _sanitize_provider_text(name, 120, fallback=None)
                if isinstance(name, str)
                else None
            )
            if safe_name is None:
                continue
            safe_properties[safe_name] = _safe_schema(child, depth=depth + 1)
        result["properties"] = safe_properties
    if "items" in value:
        result["items"] = _safe_schema(value.get("items"), depth=depth + 1)
    return result or None


def _request_body_schema(request_body: Any) -> Any:
    if not isinstance(request_body, dict) or "$ref" in request_body:
        return None
    content = request_body.get("content")
    if not isinstance(content, dict):
        return None
    media = content.get("application/json")
    return media.get("schema") if isinstance(media, dict) else None


def _response_statuses(responses: Any) -> list[str]:
    if not isinstance(responses, dict):
        return []
    safe_statuses: list[str] = []
    for status in responses:
        if not isinstance(status, (str, int)):
            continue
        safe_status = _sanitize_provider_text(str(status), 16, fallback=None)
        if safe_status is None or not (
            safe_status == "default" or safe_status.isdigit()
        ):
            continue
        safe_statuses.append(safe_status)
    return safe_statuses[:30]


def _safe_text(value: Any, limit: int) -> str | None:
    return _sanitize_provider_text(value, limit)


def _sanitize_provider_text(
    value: Any,
    limit: int,
    *,
    fallback: str | None = "[redacted]",
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\x00", " ").strip()
    if not normalized:
        return ""
    if (
        ABSOLUTE_URL_PATTERN.search(normalized)
        or CREDENTIAL_VALUE_PATTERN.search(normalized)
        or PROVIDER_SENSITIVE_TEXT_PATTERN.search(normalized)
    ):
        return fallback
    return normalized[:limit]


def _matching_template(
    method: str,
    path: str,
    allowed: dict[tuple[str, str], set[str]],
) -> str | None:
    for operation_method, template in allowed:
        if operation_method != method:
            continue
        pattern = re.sub(r"\\\{[^{}]+\\\}", "[^/]+", re.escape(template))
        if re.fullmatch(pattern, path):
            return template
    return None


def _response_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise _invalid_output()
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    return part["text"]
    raise _invalid_output()


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            SENSITIVE_FIELD_PATTERN.search(str(key))
            or _contains_sensitive_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _boundary_value(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return -1
    return ""


def _safe_sample(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return "sample"
    schema_type = schema.get("type")
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return [_safe_sample(schema.get("items"))]
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})
        return {
            name: _safe_sample(child)
            for name, child in properties.items()
            if not required or name in required
        }
    return "sample"


def _preferred_success_status(statuses: list[str]) -> int:
    numeric = sorted(
        int(status)
        for status in statuses
        if status.isdigit() and 200 <= int(status) <= 299
    )
    if numeric:
        return numeric[0]
    return 200


def _invalid_output() -> AiAssistantError:
    return AiAssistantError(
        "AI_PROVIDER_INVALID_OUTPUT",
        "AI provider returned an unsafe or invalid candidate",
        status_code=502,
    )
