from __future__ import annotations

import re

import pytest

from app.schemas import (
    OpenApiGenerateRequest,
    TestRunRequest as ApiTestRunRequest,
)
from app.services.openapi_generator import generate_openapi_cases


def operation(
    operation_id: str,
    *,
    summary: str | None = None,
    responses: dict | None = None,
    **extra,
) -> dict:
    value = {
        "operationId": operation_id,
        "responses": responses
        or {
            "200": {
                "description": "synthetic success",
            }
        },
    }
    if summary is not None:
        value["summary"] = summary
    value.update(extra)
    return value


def document(
    paths: dict,
    *,
    version: str = "3.0.3",
    server_url: str = "https://api.example.test",
    **extra,
) -> dict:
    value = {
        "openapi": version,
        "servers": [{"url": server_url}],
        "info": {"title": "Synthetic API", "version": "1.0.0"},
        "paths": paths,
    }
    value.update(extra)
    return value


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_generator_supports_openapi_30_and_31_and_returns_a_valid_run(
    version: str,
) -> None:
    payload = OpenApiGenerateRequest(
        document=document(
            {
                "/health": {
                    "get": operation("health_check"),
                }
            },
            version=version,
        )
    )

    generated = generate_openapi_cases(payload)

    assert generated.generated_count == 1
    assert generated.skipped_count == 0
    assert generated.warnings == []
    assert ApiTestRunRequest.model_validate(
        generated.run.model_dump(mode="json")
    )


def test_generation_order_ids_names_and_status_assertions_are_stable() -> None:
    spec = document(
        {
            "/first": {
                # Deliberately insert POST before GET. The frozen method order
                # still requires GET before POST.
                "post": operation(
                    "duplicate id",
                    summary="Create first",
                    responses={
                        "204": {"description": "later success"},
                        "200": {"description": "lowest success"},
                        "default": {"description": "ignored"},
                    },
                ),
                "get": operation("duplicate id", summary="Read first"),
            },
            "/second": {
                "delete": operation("delete/second"),
            },
        }
    )
    payload = OpenApiGenerateRequest(document=spec)

    first = generate_openapi_cases(payload)
    second = generate_openapi_cases(payload)

    first_cases = first.run.cases
    second_cases = second.run.cases
    assert [case.method.value for case in first_cases] == [
        "GET",
        "POST",
        "DELETE",
    ]
    assert [case.name for case in first_cases] == [
        "Read first",
        "Create first",
        "delete/second",
    ]
    assert [case.model_dump() for case in first_cases] == [
        case.model_dump() for case in second_cases
    ]

    ids = [case.id for case in first_cases]
    assert len(ids) == len(set(ids))
    assert all(
        case_id is not None
        and len(case_id) <= 64
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", case_id)
        for case_id in ids
    )
    assert re.search(r"\d+$", ids[1] or "")
    post_status = first_cases[1].assertions[0]
    assert post_status.type.value == "status_code"
    assert post_status.expected == 200


def test_operation_parameters_override_path_parameters_and_examples_win(
) -> None:
    spec = document(
        {
            "/users/{user_id}": {
                "parameters": [
                    {
                        "name": "user_id",
                        "in": "path",
                        "required": True,
                        "example": "path-level",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "tenant",
                        "in": "query",
                        "required": True,
                        "example": "path-query",
                        "schema": {"type": "string"},
                    },
                ],
                "get": operation(
                    "parameter_priority",
                    parameters=[
                        {
                            "name": "user_id",
                            "in": "path",
                            "required": True,
                            "example": "A/B 中文",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "tenant",
                            "in": "query",
                            "required": True,
                            "schema": {"example": "schema-example"},
                        },
                        {
                            "name": "from_parameter",
                            "in": "query",
                            "required": True,
                            "example": "parameter-example",
                            "schema": {
                                "example": "ignored-schema-example",
                                "default": "ignored-default",
                            },
                        },
                        {
                            "name": "from_default",
                            "in": "query",
                            "required": True,
                            "schema": {"default": "schema-default"},
                        },
                        {
                            "name": "fallback_integer",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer"},
                        },
                        {
                            "name": "optional_query",
                            "in": "query",
                            "required": False,
                            "example": "must-not-be-generated",
                            "schema": {"type": "string"},
                        },
                    ],
                ),
            }
        }
    )

    first = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    second = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    generated_case = first.run.cases[0]

    assert generated_case.path == "/users/A%2FB%20%E4%B8%AD%E6%96%87"
    assert generated_case.query["tenant"] == "schema-example"
    assert generated_case.query["from_parameter"] == "parameter-example"
    assert generated_case.query["from_default"] == "schema-default"
    assert isinstance(generated_case.query["fallback_integer"], int)
    assert "optional_query" not in generated_case.query
    assert generated_case.query == second.run.cases[0].query


def test_json_request_body_uses_media_example_before_schema_example() -> None:
    spec = document(
        {
            "/media-example": {
                "post": operation(
                    "media_example",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {"source": "media"},
                                "schema": {
                                    "example": {"source": "schema"},
                                    "type": "object",
                                },
                            }
                        },
                    },
                )
            },
            "/schema-example": {
                "put": operation(
                    "schema_example",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "example": {"source": "schema"},
                                    "type": "object",
                                }
                            }
                        },
                    },
                )
            },
        }
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.run.cases[0].json_body == {"source": "media"}
    assert generated.run.cases[1].json_body == {"source": "schema"}


def test_local_component_refs_resolve_but_external_circular_and_deep_refs_skip(
) -> None:
    parameter_components: dict[str, dict] = {
        "LocalId": {
            "name": "item_id",
            "in": "path",
            "required": True,
            "schema": {"type": "integer", "default": 7},
        },
        "CycleA": {"$ref": "#/components/parameters/CycleB"},
        "CycleB": {"$ref": "#/components/parameters/CycleA"},
    }
    for index in range(10):
        parameter_components[f"Depth{index}"] = (
            {"$ref": f"#/components/parameters/Depth{index + 1}"}
            if index < 9
            else {
                "name": "deep",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    spec = document(
        {
            "/items/{item_id}": {
                "get": operation(
                    "local_ref",
                    parameters=[
                        {"$ref": "#/components/parameters/LocalId"},
                    ],
                )
            },
            "/cycle": {
                "get": operation(
                    "cycle_ref",
                    parameters=[
                        {"$ref": "#/components/parameters/CycleA"},
                    ],
                )
            },
            "/external": {
                "get": operation(
                    "external_ref",
                    parameters=[
                        {
                            "$ref": (
                                "https://unreachable.example.test/"
                                "parameters.json#/Synthetic"
                            )
                        },
                    ],
                )
            },
            "/too-deep": {
                "get": operation(
                    "deep_ref",
                    parameters=[
                        {"$ref": "#/components/parameters/Depth0"},
                    ],
                )
            },
        },
        components={"parameters": parameter_components},
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.run.cases[0].path == "/items/7"
    assert generated.skipped_count == 3
    assert len(generated.warnings) >= 3
    warning_locations = " ".join(item.location for item in generated.warnings)
    assert "/cycle" in warning_locations
    assert "/external" in warning_locations
    assert "/too-deep" in warning_locations


def test_secured_operation_generates_no_credentials_and_emits_warning() -> None:
    synthetic_secret = "synthetic-openapi-key-do-not-return"
    synthetic_cookie = "synthetic-cookie-do-not-return"
    spec = document(
        {
            "/secured": {
                "get": operation(
                    "secured_operation",
                    security=[{"SyntheticApiKey": []}],
                    parameters=[
                        {
                            "name": "X-Synthetic-Key",
                            "in": "header",
                            "required": True,
                            "example": synthetic_secret,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "synthetic_session",
                            "in": "cookie",
                            "required": True,
                            "example": synthetic_cookie,
                            "schema": {"type": "string"},
                        },
                    ],
                )
            }
        },
        components={
            "securitySchemes": {
                "SyntheticApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Synthetic-Key",
                    "description": synthetic_secret,
                }
            }
        },
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    generated_case = generated.run.cases[0]
    assert generated_case.headers == {}
    assert generated.run.variables == {}
    assert generated.run.secret_variables == []
    assert generated.warnings
    serialized = generated.model_dump_json()
    assert synthetic_secret not in serialized
    assert synthetic_cookie not in serialized
    assert "X-Synthetic-Key" not in serialized
    assert "synthetic_session" not in serialized
    assert any(
        "security" in warning.message.lower()
        or "credential" in warning.message.lower()
        or "认证" in warning.message
        or "凭据" in warning.message
        for warning in generated.warnings
    )


def test_recursive_schema_sampling_is_structural_stable_and_json_compatible(
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "identifier": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "nothing": {"type": "null"},
            "nullable_name": {"type": ["null", "string"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "nested": {
                "properties": {
                    "choice": {"enum": ["first", "second"]},
                    "fixed": {"const": "constant"},
                    "defaulted": {"default": "default-value"},
                }
            },
            "composed": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            },
        },
    }
    spec = document(
        {
            "/objects": {
                "post": operation(
                    "recursive_body",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": schema,
                            }
                        },
                    },
                )
            }
        }
    )

    first = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    second = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    body = first.run.cases[0].json_body

    assert body == second.run.cases[0].json_body
    assert set(body) == set(schema["properties"])
    assert isinstance(body["identifier"], int)
    assert isinstance(body["ratio"], float)
    assert isinstance(body["enabled"], bool)
    assert body["nothing"] is None
    assert isinstance(body["nullable_name"], str)
    assert isinstance(body["tags"], list) and body["tags"]
    assert isinstance(body["nested"], dict)
    assert isinstance(body["composed"], str)


def test_local_request_body_and_schema_refs_generate_a_body() -> None:
    spec = document(
        {
            "/referenced": {
                "post": operation(
                    "referenced_body",
                    requestBody={
                        "$ref": "#/components/requestBodies/SyntheticBody"
                    },
                )
            }
        },
        components={
            "requestBodies": {
                "SyntheticBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/SyntheticObject"
                            }
                        }
                    },
                }
            },
            "schemas": {
                "SyntheticObject": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "example": "synthetic-name",
                        }
                    },
                }
            },
        },
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.run.cases[0].json_body == {
        "name": "synthetic-name"
    }


@pytest.mark.parametrize(
    "request_body",
    [
        {
            "required": True,
            "content": {"text/plain": {"example": "unsupported"}},
        },
        {
            "required": True,
            "content": {"application/json": "not-an-object"},
        },
        {
            "required": True,
            "content": {"application/json": {}},
        },
        {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "array"},
                }
            },
        },
    ],
)
def test_required_json_body_that_cannot_be_generated_skips_the_operation(
    request_body: dict,
) -> None:
    spec = document(
        {
            "/valid": {"get": operation("valid")},
            "/invalid-body": {
                "post": operation(
                    "invalid_body",
                    requestBody=request_body,
                )
            },
        }
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.skipped_count == 1
    assert any(
        "/invalid-body" in warning.location
        for warning in generated.warnings
    )


def test_optional_unresolvable_json_body_is_omitted_with_warning() -> None:
    spec = document(
        {
            "/optional": {
                "patch": operation(
                    "optional_body",
                    requestBody={
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/DoesNotExist"
                                    )
                                }
                            }
                        },
                    },
                )
            }
        }
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.run.cases[0].json_body is None
    assert generated.warnings
    assert any(
        warning.code == "OPTIONAL_JSON_BODY_OMITTED"
        for warning in generated.warnings
    )


def test_missing_path_samples_and_malformed_operations_skip_with_warnings(
) -> None:
    spec = document(
        {
            "/unresolved/{item_id}": {
                "get": operation("unresolved_path"),
            },
            "/invalid-operation": {
                "post": "not-an-operation",
            },
            "/invalid-parameters": {
                "put": operation(
                    "invalid_parameters",
                    parameters="not-an-array",
                ),
            },
            "/valid": {
                "get": operation("valid"),
            },
        }
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.skipped_count == 3
    codes = {warning.code for warning in generated.warnings}
    assert {
        "UNRESOLVED_PATH_PARAMETER",
        "INVALID_OPERATION",
        "INVALID_PARAMETERS",
    }.issubset(codes)


def test_fallback_and_long_case_ids_are_valid_unique_and_stable() -> None:
    long_operation_id = "9" + ("a" * 100)
    spec = document(
        {
            "/without-operation-id": {
                "get": {
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/numeric-long-one": {
                "get": operation(long_operation_id),
            },
            "/numeric-long-two": {
                "get": operation(long_operation_id),
            },
        }
    )

    first = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    second = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    ids = [case.id for case in first.run.cases]

    assert ids == [case.id for case in second.run.cases]
    assert len(ids) == len(set(ids))
    assert all(
        case_id is not None
        and len(case_id) <= 64
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", case_id)
        for case_id in ids
    )


def test_high_branch_schema_exceeding_node_budget_skips_stably_without_echo(
) -> None:
    synthetic_secret = "synthetic-node-budget-example-do-not-return"
    branching_properties = {
        f"group_{group}": {
            "type": "object",
            "properties": {
                f"value_{item}": {
                    "type": "string",
                    **(
                        {"example": synthetic_secret}
                        if group == 49 and item == 49
                        else {}
                    ),
                }
                for item in range(50)
            },
        }
        for group in range(50)
    }
    spec = document(
        {
            "/valid": {"get": operation("valid")},
            "/node-budget": {
                "post": operation(
                    "node_budget",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": branching_properties,
                                }
                            }
                        },
                    },
                )
            },
        }
    )

    first = generate_openapi_cases(OpenApiGenerateRequest(document=spec))
    second = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert first.generated_count == 1
    assert first.skipped_count == 1
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert synthetic_secret not in first.model_dump_json()
    assert any(
        "/node-budget" in warning.location
        for warning in first.warnings
    )


@pytest.mark.parametrize(
    "large_example",
    [
        "synthetic-single-large-example-do-not-return" + ("x" * 270_000),
        {
            "first": (
                "synthetic-repeated-large-example-do-not-return"
                + ("a" * 135_000)
            ),
            "second": (
                "synthetic-repeated-large-example-do-not-return"
                + ("b" * 135_000)
            ),
        },
    ],
    ids=["single-large-example", "repeated-large-example"],
)
def test_oversized_serialized_case_is_skipped_without_echoing_example(
    large_example,
) -> None:
    spec = document(
        {
            "/valid": {"get": operation("valid")},
            "/oversized-case": {
                "post": operation(
                    "oversized_case",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": large_example,
                            }
                        },
                    },
                )
            },
        }
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.skipped_count == 1
    serialized = generated.model_dump_json()
    assert "synthetic-single-large-example-do-not-return" not in serialized
    assert "synthetic-repeated-large-example-do-not-return" not in serialized
    assert any(
        "/oversized-case" in warning.location
        for warning in generated.warnings
    )


def test_schema_ref_visit_state_detects_cycle_during_recursive_traversal(
) -> None:
    spec = document(
        {
            "/valid": {"get": operation("valid")},
            "/schema-cycle": {
                "post": operation(
                    "schema_cycle",
                    requestBody={
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/CycleA"
                                }
                            }
                        },
                    },
                )
            },
        },
        components={
            "schemas": {
                "CycleA": {
                    "type": "object",
                    "properties": {
                        "next": {
                            "$ref": "#/components/schemas/CycleB"
                        }
                    },
                },
                "CycleB": {
                    "type": "object",
                    "properties": {
                        "next": {
                            "$ref": "#/components/schemas/CycleA"
                        }
                    },
                },
            }
        },
    )

    generated = generate_openapi_cases(OpenApiGenerateRequest(document=spec))

    assert generated.generated_count == 1
    assert generated.skipped_count == 1
    assert any(
        warning.code == "CIRCULAR_REFERENCE"
        and "/schema-cycle" in warning.location
        for warning in generated.warnings
    )
