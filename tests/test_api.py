from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_demo_user() -> None:
    response = client.get("/api/v1/demo/users/7")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == 7


def test_run_rejects_absolute_case_url() -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "base_url": "https://example.test",
            "cases": [
                {
                    "name": "unsafe URL",
                    "method": "GET",
                    "path": "https://other.example.test/users",
                    "assertions": [{"type": "status_code", "expected": 200}],
                }
            ],
        },
    )

    assert response.status_code == 422


def valid_run_payload() -> dict:
    return {
        "base_url": "https://example.test",
        "cases": [
            {
                "id": "health",
                "name": "health",
                "method": "GET",
                "path": "/health",
                "assertions": [{"type": "status_code", "expected": 200}],
            }
        ],
    }


def test_run_rejects_disallowed_target_before_execution() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        allowed_target_origins="",
        allow_local_targets=False,
    )
    try:
        response = client.post("/api/v1/runs", json=valid_run_payload())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_NOT_ALLOWED"


def test_run_rejects_json_assertion_without_path() -> None:
    payload = valid_run_payload()
    payload["cases"][0]["assertions"] = [
        {"type": "json_equals", "expected": 42}
    ]

    response = client.post("/api/v1/runs", json=payload)

    assert response.status_code == 422


def test_run_rejects_invalid_dependency_structure() -> None:
    payload = valid_run_payload()
    payload["cases"][0]["depends_on"] = ["future_case"]

    response = client.post("/api/v1/runs", json=payload)

    assert response.status_code == 422
