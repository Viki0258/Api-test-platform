from enum import StrEnum
from datetime import datetime, timezone
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


VARIABLE_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
CASE_ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class AssertionType(StrEnum):
    STATUS_CODE = "status_code"
    JSON_EQUALS = "json_equals"
    RESPONSE_TIME_MS = "response_time_ms"


class RunJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AssertionRule(BaseModel):
    type: AssertionType
    expected: Any
    path: str | None = None

    @model_validator(mode="after")
    def require_path_for_json_assertion(self):
        if self.type == AssertionType.JSON_EQUALS and not self.path:
            raise ValueError("path is required for json_equals")
        return self


class ExtractionRule(BaseModel):
    name: str = Field(pattern=VARIABLE_NAME_PATTERN)
    path: str = Field(min_length=1, max_length=256)
    secret: bool = False


class TestCase(BaseModel):
    id: str | None = Field(default=None, pattern=CASE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    method: HttpMethod
    path: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    json_body: Any | None = None
    assertions: list[AssertionRule] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    extract: list[ExtractionRule] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("path must be a relative API path beginning with '/'")
        return value

    @field_validator("depends_on")
    @classmethod
    def validate_dependency_names(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("depends_on entries must be unique")
        if any(not re.fullmatch(CASE_ID_PATTERN, item) for item in value):
            raise ValueError("depends_on contains an invalid case id")
        return value

    @model_validator(mode="after")
    def validate_extraction_names(self):
        names = [rule.name for rule in self.extract]
        if len(names) != len(set(names)):
            raise ValueError("extract variable names must be unique within a case")
        return self


class TestRunRequest(BaseModel):
    base_url: HttpUrl
    variables: dict[str, Any] = Field(default_factory=dict)
    secret_variables: list[str] = Field(default_factory=list)
    cases: list[TestCase] = Field(min_length=1, max_length=50)

    @field_validator("variables")
    @classmethod
    def validate_variable_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(not re.fullmatch(VARIABLE_NAME_PATTERN, name) for name in value):
            raise ValueError("variables contains an invalid variable name")
        return value

    @model_validator(mode="after")
    def validate_run_structure(self):
        parsed_base_url = (
            urlsplit(self.base_url)
            if isinstance(self.base_url, str)
            else self.base_url
        )
        if parsed_base_url.username or parsed_base_url.password:
            raise ValueError("base_url credentials are forbidden")
        if parsed_base_url.fragment:
            raise ValueError("base_url fragment is forbidden")
        if len(self.secret_variables) != len(set(self.secret_variables)):
            raise ValueError("secret_variables entries must be unique")
        missing_secrets = set(self.secret_variables) - set(self.variables)
        if missing_secrets:
            raise ValueError("each secret variable must exist in variables")

        seen: set[str] = set()
        for index, case in enumerate(self.cases, start=1):
            case_id = case.id or f"case_{index}"
            if case_id in seen:
                raise ValueError(f"duplicate case id: {case_id}")
            unknown = set(case.depends_on) - seen
            if unknown:
                raise ValueError(
                    f"case {case_id} dependencies must reference earlier cases"
                )
            seen.add(case_id)
        return self


class CandidateEvaluationRun(TestRunRequest):
    cases: list[TestCase] = Field(min_length=1, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def coerce_existing_run(cls, value):
        if isinstance(value, TestRunRequest):
            return value.model_dump(mode="python")
        return value


class AssertionResult(BaseModel):
    type: AssertionType
    passed: bool
    expected: Any
    actual: Any = None
    message: str


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CaseResult(BaseModel):
    id: str
    name: str
    status: CaseStatus
    passed: bool
    status_code: int | None = None
    response_time_ms: float
    assertions: list[AssertionResult]
    error_code: str | None = None
    error: str | None = None
    skip_reason: str | None = None
    extracted_variables: list[str] = Field(default_factory=list)


class TestRunResult(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    skipped_count: int
    duration_ms: float
    cases: list[CaseResult]

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class AsyncRunAccepted(BaseModel):
    run_id: UUID
    status: RunJobState
    status_url: str = Field(min_length=1, max_length=512)
    created_at: datetime

    @field_validator("run_id")
    @classmethod
    def require_uuidv4(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("run_id must be UUIDv4")
        return value

    @field_validator("status")
    @classmethod
    def require_queued_status(cls, value: RunJobState) -> RunJobState:
        if value is not RunJobState.QUEUED:
            raise ValueError("accepted status must be queued")
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    model_config = {"extra": "forbid"}


class AsyncRunStatus(BaseModel):
    run_id: UUID
    status: RunJobState
    attempt: int = Field(ge=0)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=500)

    @field_validator("run_id")
    @classmethod
    def require_uuidv4(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("run_id must be UUIDv4")
        return value

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    model_config = {"extra": "forbid"}


class TestRunSummary(BaseModel):
    run_id: UUID
    created_at: datetime
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    skipped_count: int
    duration_ms: float

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class TestRunHistoryList(BaseModel):
    items: list[TestRunSummary]
    limit: int
    total: int


class OpenApiGenerateRequest(BaseModel):
    document: dict[str, Any]
    base_url: str | None = None
    max_cases: int = Field(default=20, ge=1, le=50)


class OpenApiGenerationWarning(BaseModel):
    location: str
    code: str
    message: str


class OpenApiGenerateResponse(BaseModel):
    generated_count: int
    skipped_count: int
    warnings: list[OpenApiGenerationWarning]
    run: TestRunRequest


class AiGenerateRequest(BaseModel):
    document: dict[str, Any]
    base_url: str | None = None
    objective: str = Field(
        default="生成高价值边界和异常接口测试候选用例",
        min_length=1,
        max_length=500,
    )
    max_cases: int = Field(default=5, ge=1, le=10)

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective must not be blank")
        return normalized


class AiProviderStatus(BaseModel):
    provider: str
    configured: bool
    model: str | None = None
    network_access: bool


class AiCaseInsight(BaseModel):
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    category: Literal["boundary", "negative", "robustness"]
    rationale: str = Field(min_length=1, max_length=500)


class AiGenerateResponse(BaseModel):
    provider: str
    model: str | None = None
    generated_count: int
    warnings: list[OpenApiGenerationWarning]
    requires_human_review: bool = True
    insights: list[AiCaseInsight]
    run: TestRunRequest


class AiEvaluationRole(StrEnum):
    CANDIDATE_EVALUATOR = "candidate_evaluator"
    DRAFT_EVALUATOR = "draft_evaluator"


class AiEvaluationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AiEvaluationIssue(BaseModel):
    model_config = {"extra": "forbid"}

    case_id: str | None = Field(pattern=CASE_ID_PATTERN)
    severity: AiEvaluationSeverity
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(min_length=1, max_length=500)


class AiCandidateEvaluationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    document: dict[str, Any]
    candidate_run: CandidateEvaluationRun
    insights: list[AiCaseInsight] = Field(max_length=10)
    objective: str = Field(default="", max_length=500)


class AiDraftEvaluationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    draft: TestRunRequest
    document: dict[str, Any] | None = None
    objective: str = Field(default="", max_length=500)


class AiEvaluationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    role: AiEvaluationRole
    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(max_length=128)
    score: int = Field(ge=0, le=100)
    summary: str = Field(min_length=1, max_length=500)
    strengths: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        max_length=10
    )
    issues: list[AiEvaluationIssue] = Field(max_length=50)
    recommendations: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        max_length=10
    )
    evaluated_case_count: int = Field(ge=0, le=50)
    requires_human_review: Literal[True]
