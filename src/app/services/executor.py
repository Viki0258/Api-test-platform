from collections.abc import Callable
from time import perf_counter
from typing import Any

import httpx

from app.schemas import (
    AssertionResult,
    AssertionRule,
    AssertionType,
    CaseResult,
    CaseStatus,
    ExtractionRule,
    TestCase,
    TestRunResult,
)
from app.services.templating import TemplateError, render_template


REDACTION_MARKER = "[REDACTED]"


def read_json_path(document: Any, path: str) -> Any:
    """Read a simple dot-separated path such as ``data.user.id``."""
    current = document
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(path) from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def redact(value: Any, secret_values: list[Any]) -> Any:
    """Remove declared secret values from structured result fields."""
    for secret in secret_values:
        if value == secret and secret is not None:
            return REDACTION_MARKER
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            if isinstance(secret, str) and secret:
                redacted = redacted.replace(secret, REDACTION_MARKER)
        return redacted
    if isinstance(value, list):
        return [redact(item, secret_values) for item in value]
    if isinstance(value, dict):
        return {
            key: redact(item, secret_values)
            for key, item in value.items()
        }
    return value


class TestExecutor:
    def __init__(
        self,
        timeout_seconds: float = 10.0,
        run_budget_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.run_budget_seconds = run_budget_seconds
        self.transport = transport
        self.clock = clock

    def run(
        self,
        base_url: str,
        cases: list[TestCase],
        variables: dict[str, Any] | None = None,
        secret_variables: list[str] | None = None,
    ) -> TestRunResult:
        started_at = self.clock()
        context = dict(variables or {})
        secret_names = set(secret_variables or [])
        statuses: dict[str, CaseStatus] = {}
        results: list[CaseResult] = []

        with httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=self.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for index, case in enumerate(cases, start=1):
                case_id = case.id or f"case_{index}"
                failed_dependencies = [
                    dependency
                    for dependency in case.depends_on
                    if statuses.get(dependency) != CaseStatus.PASSED
                ]
                if failed_dependencies:
                    result = CaseResult(
                        id=case_id,
                        name=case.name,
                        status=CaseStatus.SKIPPED,
                        passed=False,
                        response_time_ms=0.0,
                        assertions=[],
                        error_code="DEPENDENCY_FAILED",
                        skip_reason="one or more dependencies did not pass",
                    )
                else:
                    remaining_seconds = (
                        self.run_budget_seconds - (self.clock() - started_at)
                    )
                    if remaining_seconds <= 0:
                        result = self._failed_without_request(
                            case_id,
                            case.name,
                            "RUN_BUDGET_EXCEEDED",
                            "run time budget was exhausted",
                        )
                    else:
                        result, published, new_secret_names = self._run_case(
                            client,
                            case_id,
                            case,
                            context,
                            secret_names,
                            min(self.timeout_seconds, remaining_seconds),
                        )
                        if result.status == CaseStatus.PASSED:
                            context.update(published)
                            secret_names.update(new_secret_names)

                statuses[case_id] = result.status
                results.append(result)

        passed_count = sum(
            result.status == CaseStatus.PASSED for result in results
        )
        skipped_count = sum(
            result.status == CaseStatus.SKIPPED for result in results
        )
        failed_count = len(results) - passed_count - skipped_count
        return TestRunResult(
            passed=passed_count == len(results),
            total=len(results),
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            duration_ms=round((self.clock() - started_at) * 1000, 2),
            cases=results,
        )

    def _run_case(
        self,
        client: httpx.Client,
        case_id: str,
        case: TestCase,
        variables: dict[str, Any],
        secret_names: set[str],
        timeout_seconds: float,
    ) -> tuple[CaseResult, dict[str, Any], set[str]]:
        started_at = self.clock()
        initial_secret_values = [
            variables[name] for name in secret_names if name in variables
        ]
        try:
            path = render_template(case.path, variables)
            headers = render_template(case.headers, variables)
            query = render_template(case.query, variables)
            json_body = render_template(case.json_body, variables)
            assertions = [
                rule.model_copy(
                    update={
                        "expected": render_template(rule.expected, variables)
                    }
                )
                for rule in case.assertions
            ]
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or path.startswith("//")
            ):
                raise TemplateError(
                    "rendered path must remain a relative API path"
                )
            normalized_headers = self._normalize_headers(headers)
        except TemplateError as exc:
            return (
                self._failed_without_request(
                    case_id,
                    case.name,
                    exc.code,
                    "unable to render request template",
                ),
                {},
                set(),
            )

        try:
            response = client.request(
                method=case.method.value,
                url=path,
                headers=normalized_headers,
                params=query,
                json=json_body,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException:
            return (
                self._network_failure(
                    case_id, case.name, started_at, "REQUEST_TIMEOUT"
                ),
                {},
                set(),
            )
        except httpx.ConnectError:
            return (
                self._network_failure(
                    case_id,
                    case.name,
                    started_at,
                    "CONNECT_FAILED",
                    "ConnectError: request could not be completed",
                ),
                {},
                set(),
            )
        except (httpx.HTTPError, TypeError, ValueError):
            return (
                self._network_failure(
                    case_id, case.name, started_at, "NETWORK_ERROR"
                ),
                {},
                set(),
            )

        elapsed_ms = round((self.clock() - started_at) * 1000, 2)
        extracted, extraction_secrets, extraction_error = self._extract(
            response,
            case.extract,
            variables,
        )
        all_secret_values = initial_secret_values + [
            extracted[name]
            for name in extraction_secrets
            if name in extracted
        ]
        assertion_results = [
            self._evaluate(rule, response, elapsed_ms, all_secret_values)
            for rule in assertions
        ]

        assertions_passed = all(item.passed for item in assertion_results)
        passed = extraction_error is None and assertions_passed
        error_code = None
        error = None
        if extraction_error is not None:
            error_code = "EXTRACTION_FAILED"
            error = extraction_error
        elif not assertions_passed:
            error_code = "ASSERTION_FAILED"
            error = "one or more assertions failed"

        published = extracted if passed else {}
        published_secrets = extraction_secrets if passed else set()
        return (
            CaseResult(
                id=case_id,
                name=case.name,
                status=CaseStatus.PASSED if passed else CaseStatus.FAILED,
                passed=passed,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
                assertions=assertion_results,
                error_code=error_code,
                error=error,
                extracted_variables=list(published),
            ),
            published,
            published_secrets,
        )

    @staticmethod
    def _normalize_headers(headers: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, value in headers.items():
            if isinstance(value, (dict, list)) or value is None:
                raise TemplateError("header values must be scalar")
            if isinstance(value, bool):
                normalized[name] = "true" if value else "false"
            else:
                normalized[name] = str(value)
        return normalized

    @staticmethod
    def _extract(
        response: httpx.Response,
        rules: list[ExtractionRule],
        existing_variables: dict[str, Any],
    ) -> tuple[dict[str, Any], set[str], str | None]:
        if not rules:
            return {}, set(), None
        try:
            document = response.json()
        except ValueError:
            return {}, set(), "response body is not valid JSON"

        extracted: dict[str, Any] = {}
        secret_names: set[str] = set()
        for rule in rules:
            if rule.name in existing_variables or rule.name in extracted:
                return {}, set(), "extracted variable would overwrite a value"
            try:
                extracted[rule.name] = read_json_path(document, rule.path)
            except KeyError:
                return {}, set(), "required JSON extraction path was not found"
            if rule.secret:
                secret_names.add(rule.name)
        return extracted, secret_names, None

    @staticmethod
    def _failed_without_request(
        case_id: str,
        name: str,
        error_code: str,
        error: str,
    ) -> CaseResult:
        return CaseResult(
            id=case_id,
            name=name,
            status=CaseStatus.FAILED,
            passed=False,
            response_time_ms=0.0,
            assertions=[],
            error_code=error_code,
            error=error,
        )

    def _network_failure(
        self,
        case_id: str,
        name: str,
        started_at: float,
        error_code: str,
        error: str = "request could not be completed",
    ) -> CaseResult:
        return CaseResult(
            id=case_id,
            name=name,
            status=CaseStatus.FAILED,
            passed=False,
            response_time_ms=round((self.clock() - started_at) * 1000, 2),
            assertions=[],
            error_code=error_code,
            error=error,
        )

    @staticmethod
    def _evaluate(
        rule: AssertionRule,
        response: httpx.Response,
        elapsed_ms: float,
        secret_values: list[Any],
    ) -> AssertionResult:
        if rule.type == AssertionType.STATUS_CODE:
            actual = response.status_code
            passed = actual == rule.expected
            message = "status code assertion passed" if passed else (
                "status code assertion failed"
            )
        elif rule.type == AssertionType.RESPONSE_TIME_MS:
            actual = elapsed_ms
            try:
                passed = actual <= float(rule.expected)
            except (TypeError, ValueError):
                passed = False
            message = "response time assertion passed" if passed else (
                "response time assertion failed"
            )
        else:
            try:
                actual = read_json_path(response.json(), rule.path or "")
                passed = actual == rule.expected
                message = "JSON equality assertion passed" if passed else (
                    "JSON equality assertion failed"
                )
            except (ValueError, KeyError):
                actual = None
                passed = False
                message = "unable to read the configured JSON path"

        return AssertionResult(
            type=rule.type,
            passed=passed,
            expected=redact(rule.expected, secret_values),
            actual=redact(actual, secret_values),
            message=message,
        )
