from enum import StrEnum
from datetime import datetime, timezone
import re
from typing import Any
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
        if self.base_url.username or self.base_url.password:
            raise ValueError("base_url credentials are forbidden")
        if self.base_url.fragment:
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
