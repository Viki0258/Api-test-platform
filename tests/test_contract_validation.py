from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, normalize_origin, target_is_allowed
from app.schemas import TestRunRequest as ApiTestRunRequest


def valid_case(**overrides):
    payload = {
        "id": "first",
        "name": "synthetic case",
        "method": "GET",
        "path": "/health",
        "assertions": [{"type": "status_code", "expected": 200}],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@example.test",
        "https://example.test#fragment",
    ],
)
def test_run_rejects_credentials_and_fragments_in_base_url(
    base_url: str,
) -> None:
    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {"base_url": base_url, "cases": [valid_case()]}
        )


@pytest.mark.parametrize(
    "variables",
    [
        {"": 1},
        {"1invalid": 1},
        {"invalid-name": 1},
        {"a" * 65: 1},
    ],
)
def test_run_rejects_invalid_variable_names(variables: dict) -> None:
    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {
                "base_url": "https://example.test",
                "variables": variables,
                "cases": [valid_case()],
            }
        )


def test_secret_variables_must_be_unique_and_exist() -> None:
    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {
                "base_url": "https://example.test",
                "variables": {"token": "synthetic"},
                "secret_variables": ["token", "token"],
                "cases": [valid_case()],
            }
        )

    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {
                "base_url": "https://example.test",
                "variables": {},
                "secret_variables": ["token"],
                "cases": [valid_case()],
            }
        )


def test_case_ids_are_unique_including_generated_ids() -> None:
    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {
                "base_url": "https://example.test",
                "cases": [
                    valid_case(id=None),
                    valid_case(id="case_1", name="duplicate generated id"),
                ],
            }
        )


@pytest.mark.parametrize(
    "cases",
    [
        [
            valid_case(
                id="consumer",
                depends_on=["producer"],
            ),
            valid_case(id="producer", name="later producer"),
        ],
        [
            valid_case(id="producer"),
            valid_case(
                id="consumer",
                name="duplicate dependencies",
                depends_on=["producer", "producer"],
            ),
        ],
    ],
)
def test_dependencies_must_be_unique_existing_earlier_ids(cases: list) -> None:
    with pytest.raises(ValidationError):
        ApiTestRunRequest.model_validate(
            {"base_url": "https://example.test", "cases": cases}
        )


def test_run_accepts_a_valid_dependency_chain() -> None:
    request = ApiTestRunRequest.model_validate(
        {
            "base_url": "https://example.test",
            "variables": {"tenant_id": 7},
            "secret_variables": [],
            "cases": [
                valid_case(id="producer"),
                valid_case(
                    id="consumer",
                    name="consumer",
                    depends_on=["producer"],
                ),
            ],
        }
    )

    assert request.cases[1].depends_on == ["producer"]


def test_target_policy_denies_by_default_and_matches_exact_origins() -> None:
    default_settings = Settings(
        allowed_target_origins="",
        allow_local_targets=False,
    )
    allowlist_settings = Settings(
        allowed_target_origins="https://example.test:8443",
        allow_local_targets=False,
    )

    assert target_is_allowed("https://example.test", default_settings) is False
    assert target_is_allowed("http://127.0.0.1", default_settings) is False
    assert (
        target_is_allowed(
            "https://example.test:8443/api",
            allowlist_settings,
        )
        is True
    )
    assert (
        normalize_origin(
            "https://example.test:8443/api",
            origin_only=False,
        )
        == "https://example.test:8443"
    )
    assert (
        target_is_allowed("https://example.test", allowlist_settings) is False
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_target_policy_allows_loopback_only_when_explicitly_enabled(
    base_url: str,
) -> None:
    disabled = Settings(allow_local_targets=False)
    enabled = Settings(allow_local_targets=True)

    assert target_is_allowed(base_url, disabled) is False
    assert target_is_allowed(base_url, enabled) is True


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.test",
        "https://user@example.test",
        "https://example.test?query=1",
        "https://example.test#fragment",
    ],
)
def test_origin_normalization_rejects_non_origin_values(value: str) -> None:
    assert normalize_origin(value) is None
