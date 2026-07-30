from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.schemas import (
    AiCaseInsight,
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
SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete")
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:authorization|api[-_]?key|token|secret|password|cookie|session)",
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
        prompt = json.dumps(
            {
                "objective": objective,
                "max_cases": max_cases,
                "api_structure": outline,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise AiAssistantError(
                "AI_SOURCE_DOCUMENT_TOO_LARGE",
                "sanitized API structure exceeds the AI prompt limit",
                status_code=422,
            )

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
        if self._provider is None:
            raise AiAssistantError(
                "AI_PROVIDER_NOT_CONFIGURED",
                "OpenAI provider requires OPENAI_API_KEY",
                status_code=503,
            )
        baseline = _baseline_run(request)
        outline = build_safe_outline(request.document)
        provider_output = self._provider.generate(
            outline=outline,
            objective=request.objective,
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


def build_safe_outline(document: dict[str, Any]) -> dict[str, Any]:
    try:
        serialized = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AiAssistantError(
            "INVALID_AI_SOURCE_DOCUMENT",
            "OpenAPI document must be JSON serializable",
            status_code=422,
        ) from None
    if len(serialized) > MAX_DOCUMENT_BYTES:
        raise AiAssistantError(
            "AI_SOURCE_DOCUMENT_TOO_LARGE",
            "OpenAPI document exceeds the 1 MiB limit",
            status_code=422,
        )

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
                    "path": path[:2048],
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


def _baseline_run(request: AiGenerateRequest) -> TestRunRequest:
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
            if (
                not isinstance(name, str)
                or location not in {"path", "query"}
                or SENSITIVE_FIELD_PATTERN.search(name)
            ):
                continue
            combined[(name, location)] = {
                "name": name[:120],
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
        result["type"] = schema_type[:30]
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
        if isinstance(item, (str, int, float, bool)):
            result[key] = item
    if isinstance(value.get("enum"), list):
        result["enum_count"] = len(value["enum"])
    required = value.get("required")
    if isinstance(required, list):
        result["required"] = [
            item[:120]
            for item in required
            if isinstance(item, str) and not SENSITIVE_FIELD_PATTERN.search(item)
        ][:50]
    properties = value.get("properties")
    if isinstance(properties, dict):
        safe_properties = {}
        for name, child in list(properties.items())[:50]:
            if not isinstance(name, str) or SENSITIVE_FIELD_PATTERN.search(name):
                continue
            safe_properties[name[:120]] = _safe_schema(child, depth=depth + 1)
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
    return [
        str(status)[:16]
        for status in responses
        if isinstance(status, (str, int))
    ][:30]


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


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
