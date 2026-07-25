from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from app.schemas import (
    AssertionRule,
    AssertionType,
    HttpMethod,
    OpenApiGenerateRequest,
    OpenApiGenerateResponse,
    OpenApiGenerationWarning,
    TestCase,
    TestRunRequest,
)


MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PATHS = 200
MAX_REFERENCE_DEPTH = 8
MAX_SCHEMA_DEPTH = 8
MAX_GENERATED_SCHEMA_NODES = 2048
MAX_SERIALIZED_CASE_BYTES = 262144
SUPPORTED_METHODS = ("get", "post", "put", "patch", "delete")
KNOWN_UNSUPPORTED_METHODS = ("head", "options", "trace")


class OpenApiGenerationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _OperationProblem(Exception):
    code: str
    message: str


@dataclass
class _GenerationBudget:
    remaining_schema_nodes: int = MAX_GENERATED_SCHEMA_NODES

    def consume_schema_node(self) -> None:
        if self.remaining_schema_nodes <= 0:
            raise _OperationProblem(
                "SCHEMA_NODE_BUDGET_EXCEEDED",
                "generated schema sample exceeds the supported node budget",
            )
        self.remaining_schema_nodes -= 1


class _Generator:
    def __init__(self, payload: OpenApiGenerateRequest) -> None:
        self.payload = payload
        self.document = payload.document
        self.warnings: list[OpenApiGenerationWarning] = []
        self.skipped_count = 0
        self.used_case_ids: set[str] = set()

    def generate(self) -> OpenApiGenerateResponse:
        self._validate_document()
        base_url = self._select_base_url()
        cases: list[TestCase] = []
        paths = self.document.get("paths", {})

        for path, raw_path_item in paths.items():
            location = f"paths.{path}"
            if not isinstance(path, str) or not isinstance(raw_path_item, dict):
                self._skip(
                    location,
                    "INVALID_PATH_ITEM",
                    "path item is not a supported object",
                )
                continue
            try:
                path_item = self._resolve(
                    raw_path_item,
                    location,
                    allowed_component="pathItems",
                )
            except _OperationProblem as problem:
                self._skip(location, problem.code, problem.message)
                continue

            for method in SUPPORTED_METHODS:
                if method not in path_item:
                    continue
                operation_location = f"{location}.{method}"
                if len(cases) >= self.payload.max_cases:
                    self._skip(
                        operation_location,
                        "MAX_CASES_REACHED",
                        "operation skipped because max_cases was reached",
                    )
                    continue
                try:
                    case = self._generate_case(
                        path,
                        method,
                        path_item,
                        operation_location,
                    )
                except _OperationProblem as problem:
                    self._skip(
                        operation_location,
                        problem.code,
                        problem.message,
                    )
                    continue
                cases.append(case)

            for method in KNOWN_UNSUPPORTED_METHODS:
                if method in path_item:
                    self._skip(
                        f"{location}.{method}",
                        "UNSUPPORTED_METHOD",
                        "HTTP method is not supported by the test executor",
                    )

        if not cases:
            raise OpenApiGenerationError(
                "NO_OPENAPI_CASES_GENERATED",
                "the document did not produce any supported test cases",
            )

        try:
            run = TestRunRequest(
                base_url=base_url,
                variables={},
                secret_variables=[],
                cases=cases,
            )
        except ValidationError as exc:
            raise OpenApiGenerationError(
                "NO_OPENAPI_CASES_GENERATED",
                "generated cases did not satisfy the test run contract",
            ) from exc
        return OpenApiGenerateResponse(
            generated_count=len(cases),
            skipped_count=self.skipped_count,
            warnings=self.warnings,
            run=run,
        )

    def _validate_document(self) -> None:
        try:
            size = len(
                json.dumps(
                    self.document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise OpenApiGenerationError(
                "OPENAPI_DOCUMENT_TOO_LARGE",
                "the OpenAPI document cannot be safely serialized",
            ) from exc
        if size > MAX_DOCUMENT_BYTES:
            raise OpenApiGenerationError(
                "OPENAPI_DOCUMENT_TOO_LARGE",
                "the OpenAPI document exceeds the 1 MiB limit",
            )

        version = self.document.get("openapi")
        if not isinstance(version, str) or not re.fullmatch(
            r"3\.(?:0|1)\.\d+",
            version,
        ):
            raise OpenApiGenerationError(
                "INVALID_OPENAPI_VERSION",
                "only OpenAPI 3.0.x and 3.1.x documents are supported",
            )

        paths = self.document.get("paths", {})
        if not isinstance(paths, dict):
            raise OpenApiGenerationError(
                "NO_OPENAPI_CASES_GENERATED",
                "the OpenAPI paths field must be an object",
            )
        if len(paths) > MAX_PATHS:
            raise OpenApiGenerationError(
                "OPENAPI_TOO_MANY_PATHS",
                "the OpenAPI document exceeds the 200 path limit",
            )

    def _select_base_url(self) -> str:
        selected = self.payload.base_url
        server: dict[str, Any] | None = None
        if selected is None:
            servers = self.document.get("servers")
            if isinstance(servers, list) and servers:
                first = servers[0]
                if isinstance(first, dict):
                    server = first
                    raw_url = first.get("url")
                    if isinstance(raw_url, str):
                        selected = raw_url
            if selected is None:
                raise OpenApiGenerationError(
                    "OPENAPI_BASE_URL_REQUIRED",
                    "provide base_url or a first OpenAPI server URL",
                )

        if server is not None and server.get("variables"):
            raise OpenApiGenerationError(
                "INVALID_OPENAPI_BASE_URL",
                "OpenAPI server variables are not supported",
            )
        if not self._valid_base_url(selected):
            raise OpenApiGenerationError(
                "INVALID_OPENAPI_BASE_URL",
                "base_url must be an absolute HTTP URL without credentials, "
                "fragments, or server variables",
            )
        return selected

    @staticmethod
    def _valid_base_url(value: str) -> bool:
        if not isinstance(value, str) or "{" in value or "}" in value:
            return False
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )

    def _generate_case(
        self,
        path: str,
        method: str,
        path_item: dict[str, Any],
        location: str,
    ) -> TestCase:
        raw_operation = path_item[method]
        if not isinstance(raw_operation, dict):
            raise _OperationProblem(
                "INVALID_OPERATION",
                "operation is not a supported object",
            )
        operation = self._resolve(
            raw_operation,
            location,
            allowed_component=None,
        )
        budget = _GenerationBudget()
        rendered_path, query = self._generate_parameters(
            path,
            path_item.get("parameters", []),
            operation.get("parameters", []),
            location,
            budget,
        )
        json_body = self._generate_request_body(
            operation,
            location,
            budget,
        )
        expected_status = self._success_status(operation, location)

        effective_security = operation.get(
            "security",
            self.document.get("security"),
        )
        if self._requires_credentials(effective_security):
            self._warn(
                location,
                "SECURITY_CREDENTIALS_NOT_GENERATED",
                "secured operation was generated without credentials",
            )

        case_id = self._case_id(
            operation.get("operationId"),
            method,
            path,
        )
        case_name = self._case_name(operation, method, path)
        try:
            case = TestCase(
                id=case_id,
                name=case_name,
                method=HttpMethod(method.upper()),
                path=rendered_path,
                query=query,
                json_body=json_body,
                assertions=[
                    AssertionRule(
                        type=AssertionType.STATUS_CODE,
                        expected=expected_status,
                    )
                ],
            )
        except ValidationError as exc:
            raise _OperationProblem(
                "INVALID_OPERATION",
                "operation could not produce a valid test case",
            ) from exc
        if len(case.model_dump_json().encode("utf-8")) > (
            MAX_SERIALIZED_CASE_BYTES
        ):
            raise _OperationProblem(
                "GENERATED_CASE_TOO_LARGE",
                "generated test case exceeds the supported size budget",
            )
        return case

    def _generate_parameters(
        self,
        path: str,
        path_parameters: Any,
        operation_parameters: Any,
        location: str,
        budget: _GenerationBudget,
    ) -> tuple[str, dict[str, Any]]:
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for source_name, raw_parameters in (
            ("path", path_parameters),
            ("operation", operation_parameters),
        ):
            if raw_parameters is None:
                continue
            if not isinstance(raw_parameters, list):
                raise _OperationProblem(
                    "INVALID_PARAMETERS",
                    f"{source_name} parameters must be an array",
                )
            for index, raw_parameter in enumerate(raw_parameters):
                parameter_location = (
                    f"{location}.{source_name}_parameters[{index}]"
                )
                parameter = self._resolve(
                    raw_parameter,
                    parameter_location,
                    allowed_component="parameters",
                )
                name = parameter.get("name")
                parameter_in = parameter.get("in")
                if not isinstance(name, str) or not isinstance(
                    parameter_in,
                    str,
                ):
                    raise _OperationProblem(
                        "INVALID_PARAMETER",
                        "parameter requires string name and location",
                    )
                combined[(name, parameter_in)] = parameter

        rendered_path = path
        query: dict[str, Any] = {}
        for (name, parameter_in), parameter in combined.items():
            required = parameter.get("required") is True
            if parameter_in not in {"path", "query"}:
                if required:
                    self._warn(
                        location,
                        "UNSUPPORTED_REQUIRED_PARAMETER",
                        "required header or cookie parameter was not generated",
                    )
                continue
            if not required:
                continue
            value = self._parameter_sample(parameter, location, budget)
            if parameter_in == "path":
                marker = "{" + name + "}"
                if marker not in rendered_path:
                    raise _OperationProblem(
                        "INVALID_PATH_PARAMETER",
                        "required path parameter has no matching placeholder",
                    )
                rendered_path = rendered_path.replace(
                    marker,
                    quote(self._path_text(value), safe=""),
                )
            else:
                query[name] = value

        if re.search(r"\{[^{}]+\}", rendered_path):
            raise _OperationProblem(
                "UNRESOLVED_PATH_PARAMETER",
                "path contains a parameter without a required sample",
            )
        return rendered_path, query

    def _parameter_sample(
        self,
        parameter: dict[str, Any],
        location: str,
        budget: _GenerationBudget,
    ) -> Any:
        if "example" in parameter:
            return parameter["example"]
        raw_schema = parameter.get("schema", {})
        return self._schema_sample(
            raw_schema,
            f"{location}.schema",
            0,
            set(),
            budget,
        )

    def _generate_request_body(
        self,
        operation: dict[str, Any],
        location: str,
        budget: _GenerationBudget,
    ) -> Any | None:
        if "requestBody" not in operation:
            return None
        request_body = self._resolve(
            operation["requestBody"],
            f"{location}.requestBody",
            allowed_component="requestBodies",
        )
        required = request_body.get("required") is True
        content = request_body.get("content")
        if not isinstance(content, dict) or "application/json" not in content:
            if required:
                raise _OperationProblem(
                    "REQUIRED_JSON_BODY_UNAVAILABLE",
                    "required application/json request body is unavailable",
                )
            return None
        media = content["application/json"]
        if not isinstance(media, dict):
            if required:
                raise _OperationProblem(
                    "REQUIRED_JSON_BODY_UNAVAILABLE",
                    "required application/json request body is invalid",
                )
            return None
        if "example" in media:
            return media["example"]
        if "schema" not in media:
            if required:
                raise _OperationProblem(
                    "REQUIRED_JSON_BODY_UNAVAILABLE",
                    "required application/json request body has no sample",
                )
            return None
        try:
            return self._schema_sample(
                media["schema"],
                f"{location}.requestBody.schema",
                0,
                set(),
                budget,
            )
        except _OperationProblem as problem:
            if (
                required
                or problem.code == "SCHEMA_NODE_BUDGET_EXCEEDED"
            ):
                raise
            self._warn(
                f"{location}.requestBody",
                "OPTIONAL_JSON_BODY_OMITTED",
                "optional application/json body could not be generated",
            )
            return None

    def _schema_sample(
        self,
        raw_schema: Any,
        location: str,
        depth: int,
        active_refs: set[str],
        budget: _GenerationBudget,
    ) -> Any:
        budget.consume_schema_node()
        if depth > MAX_SCHEMA_DEPTH:
            raise _OperationProblem(
                "SCHEMA_DEPTH_EXCEEDED",
                "schema recursion exceeds the supported depth",
            )
        schema, active_refs = self._resolve_schema(
            raw_schema,
            location,
            active_refs,
        )
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        if "const" in schema:
            return schema["const"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]

        for composition in ("oneOf", "anyOf"):
            options = schema.get(composition)
            if isinstance(options, list) and options:
                return self._schema_sample(
                    options[0],
                    f"{location}.{composition}[0]",
                    depth + 1,
                    set(active_refs),
                    budget,
                )

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            schema_type = next(
                (item for item in schema_type if item != "null"),
                "null",
            )
        if schema_type is None and "properties" in schema:
            schema_type = "object"
        if schema_type == "object":
            properties = schema.get("properties", {})
            if not isinstance(properties, dict):
                raise _OperationProblem(
                    "SCHEMA_SAMPLE_UNAVAILABLE",
                    "object schema properties are invalid",
                )
            return {
                name: self._schema_sample(
                    child,
                    f"{location}.properties.{name}",
                    depth + 1,
                    set(active_refs),
                    budget,
                )
                for name, child in properties.items()
                if isinstance(name, str)
            }
        if schema_type == "array":
            if "items" not in schema:
                raise _OperationProblem(
                    "SCHEMA_SAMPLE_UNAVAILABLE",
                    "array schema has no item schema",
                )
            return [
                self._schema_sample(
                    schema["items"],
                    f"{location}.items",
                    depth + 1,
                    set(active_refs),
                    budget,
                )
            ]
        if schema_type == "integer":
            return 1
        if schema_type == "number":
            return 1.0
        if schema_type == "boolean":
            return True
        if schema_type == "null":
            return None
        if schema_type == "string" or schema_type is None:
            return "sample"
        raise _OperationProblem(
            "SCHEMA_SAMPLE_UNAVAILABLE",
            "schema type has no deterministic sample",
        )

    def _resolve_schema(
        self,
        raw_value: Any,
        location: str,
        active_refs: set[str],
    ) -> tuple[dict[str, Any], set[str]]:
        if not isinstance(raw_value, dict):
            raise _OperationProblem(
                "INVALID_REFERENCE_TARGET",
                "referenced value is not a supported object",
            )
        if "$ref" not in raw_value:
            return raw_value, active_refs

        reference = raw_value["$ref"]
        if (
            not isinstance(reference, str)
            or not reference.startswith("#/components/schemas/")
        ):
            raise _OperationProblem(
                "EXTERNAL_REFERENCE_UNSUPPORTED",
                "only local schema component references are supported",
            )
        if len(active_refs) >= MAX_REFERENCE_DEPTH:
            raise _OperationProblem(
                "REFERENCE_DEPTH_EXCEEDED",
                "reference chain exceeds the supported depth",
            )
        if reference in active_refs:
            raise _OperationProblem(
                "CIRCULAR_REFERENCE",
                "circular component reference is not supported",
            )

        resolved_refs = set(active_refs)
        resolved_refs.add(reference)
        current: Any = self.document
        try:
            for encoded_part in reference[2:].split("/"):
                part = encoded_part.replace("~1", "/").replace("~0", "~")
                current = current[part]
        except (KeyError, TypeError):
            raise _OperationProblem(
                "UNRESOLVED_REFERENCE",
                "local component reference could not be resolved",
            ) from None
        resolved, resolved_refs = self._resolve_schema(
            current,
            location,
            resolved_refs,
        )
        siblings = {
            key: value for key, value in raw_value.items() if key != "$ref"
        }
        return {**resolved, **siblings}, resolved_refs

    def _resolve(
        self,
        raw_value: Any,
        location: str,
        allowed_component: str | None,
        depth: int = 0,
        seen_refs: set[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_value, dict):
            raise _OperationProblem(
                "INVALID_REFERENCE_TARGET",
                "referenced value is not a supported object",
            )
        if "$ref" not in raw_value:
            return raw_value
        reference = raw_value["$ref"]
        if not isinstance(reference, str) or not reference.startswith(
            "#/components/"
        ):
            raise _OperationProblem(
                "EXTERNAL_REFERENCE_UNSUPPORTED",
                "only local component references are supported",
            )
        parts = reference[2:].split("/")
        if (
            len(parts) < 3
            or parts[0] != "components"
            or (
                allowed_component is not None
                and parts[1] != allowed_component
            )
            or parts[1] == "securitySchemes"
        ):
            raise _OperationProblem(
                "REFERENCE_TARGET_UNSUPPORTED",
                "reference does not target an allowed component",
            )
        if depth >= MAX_REFERENCE_DEPTH:
            raise _OperationProblem(
                "REFERENCE_DEPTH_EXCEEDED",
                "reference chain exceeds the supported depth",
            )
        visited = set(seen_refs or ())
        if reference in visited:
            raise _OperationProblem(
                "CIRCULAR_REFERENCE",
                "circular component reference is not supported",
            )
        visited.add(reference)

        current: Any = self.document
        try:
            for encoded_part in parts:
                part = encoded_part.replace("~1", "/").replace("~0", "~")
                current = current[part]
        except (KeyError, TypeError):
            raise _OperationProblem(
                "UNRESOLVED_REFERENCE",
                "local component reference could not be resolved",
            ) from None
        resolved = self._resolve(
            current,
            location,
            allowed_component=allowed_component,
            depth=depth + 1,
            seen_refs=visited,
        )
        siblings = {
            key: value for key, value in raw_value.items() if key != "$ref"
        }
        return {**resolved, **siblings}

    @staticmethod
    def _success_status(
        operation: dict[str, Any],
        location: str,
    ) -> int:
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            raise _OperationProblem(
                "MISSING_SUCCESS_RESPONSE",
                "operation has no explicit numeric 2xx response",
            )
        statuses = [
            int(status)
            for status in responses
            if isinstance(status, str)
            and re.fullmatch(r"\d{3}", status)
            and 200 <= int(status) <= 299
        ]
        if not statuses:
            raise _OperationProblem(
                "MISSING_SUCCESS_RESPONSE",
                "operation has no explicit numeric 2xx response",
            )
        return min(statuses)

    @staticmethod
    def _requires_credentials(value: Any) -> bool:
        return (
            isinstance(value, list)
            and any(isinstance(item, dict) and bool(item) for item in value)
        )

    def _case_id(
        self,
        operation_id: Any,
        method: str,
        path: str,
    ) -> str:
        source = (
            operation_id
            if isinstance(operation_id, str) and operation_id
            else f"{method}_{path}"
        )
        normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", source).strip("_")
        if not normalized:
            normalized = "case"
        if not re.match(r"^[A-Za-z_]", normalized):
            normalized = f"case_{normalized}"
        normalized = normalized[:64]
        candidate = normalized
        suffix = 2
        while candidate in self.used_case_ids:
            ending = f"_{suffix}"
            candidate = f"{normalized[:64 - len(ending)]}{ending}"
            suffix += 1
        self.used_case_ids.add(candidate)
        return candidate

    @staticmethod
    def _case_name(
        operation: dict[str, Any],
        method: str,
        path: str,
    ) -> str:
        for value in (
            operation.get("summary"),
            operation.get("operationId"),
            f"{method.upper()} {path}",
        ):
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
        return f"{method.upper()} operation"

    @staticmethod
    def _path_text(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        if value is None:
            return "null"
        return str(value)

    def _skip(self, location: str, code: str, message: str) -> None:
        self.skipped_count += 1
        self._warn(location, code, message)

    def _warn(self, location: str, code: str, message: str) -> None:
        self.warnings.append(
            OpenApiGenerationWarning(
                location=location,
                code=code,
                message=message,
            )
        )


def generate_openapi_cases(
    payload: OpenApiGenerateRequest,
) -> OpenApiGenerateResponse:
    """Generate deterministic executable cases without network access."""
    return _Generator(payload).generate()
