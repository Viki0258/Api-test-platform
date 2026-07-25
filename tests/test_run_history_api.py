from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.main import app, get_run_history_store
from app.schemas import TestRunResult as ApiTestRunResult
from app.services.run_history import HistoryStorageError, RunHistoryStore
from tests.test_run_history_storage import make_result


VALID_PAYLOAD = {
    "base_url": "https://example.test",
    "variables": {
        "token": "synthetic-secret-never-persist",
    },
    "secret_variables": ["token"],
    "cases": [
        {
            "id": "history_case",
            "name": "history case",
            "method": "GET",
            "path": "/health",
            "assertions": [{"type": "status_code", "expected": 200}],
        }
    ],
}


class FakeExecutor:
    result_factory: Callable[[], ApiTestRunResult] = staticmethod(make_result)

    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, *_args, **_kwargs) -> ApiTestRunResult:
        return self.result_factory()


class FailingStore:
    def __init__(self, message: str = "raw sqlite failure at C:\\secret") -> None:
        self.message = message
        self.save_calls = 0

    def save(self, _result: ApiTestRunResult) -> None:
        self.save_calls += 1
        raise HistoryStorageError(self.message)

    def list(self, _limit: int):
        raise HistoryStorageError(self.message)

    def get(self, _run_id: UUID):
        raise HistoryStorageError(self.message)


@pytest.fixture
def isolated_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    FakeExecutor.result_factory = staticmethod(make_result)
    store = RunHistoryStore(tmp_path / "isolated" / "history.sqlite3")
    app.dependency_overrides[get_run_history_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(
        allowed_target_origins="https://example.test",
        allow_local_targets=False,
    )
    monkeypatch.setattr(main_module, "TestExecutor", FakeExecutor)
    try:
        with TestClient(app) as client:
            yield client, store
    finally:
        app.dependency_overrides.clear()


def test_post_adds_uuid4_utc_metadata_and_persists_the_result(
    isolated_client,
) -> None:
    client, store = isolated_client
    before = datetime.now(timezone.utc)

    response = client.post("/api/v1/runs", json=VALID_PAYLOAD)

    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    body = response.json()
    run_id = UUID(body["run_id"])
    created_at = datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
    assert run_id.version == 4
    assert created_at.tzinfo is not None
    assert before <= created_at <= after
    persisted = store.get(run_id)
    assert persisted is not None
    assert persisted.model_dump(mode="json") == body


def test_post_persists_failed_results(isolated_client) -> None:
    client, store = isolated_client
    FakeExecutor.result_factory = staticmethod(
        lambda: make_result(passed=False)
    )

    response = client.post("/api/v1/runs", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json()["passed"] is False
    assert store.get(UUID(response.json()["run_id"])) is not None


def test_validation_and_target_rejections_do_not_save(
    isolated_client,
) -> None:
    client, store = isolated_client
    invalid = client.post("/api/v1/runs", json={"cases": []})
    disallowed_payload = {
        **VALID_PAYLOAD,
        "base_url": "https://disallowed.example.test",
    }
    disallowed = client.post("/api/v1/runs", json=disallowed_payload)

    assert invalid.status_code == 422
    assert disallowed.status_code == 422
    assert store.list(limit=20) == ([], 0)


def test_list_is_empty_then_returns_summary_without_cases(
    isolated_client,
) -> None:
    client, _store = isolated_client

    empty = client.get("/api/v1/runs")
    created = client.post("/api/v1/runs", json=VALID_PAYLOAD)
    listing = client.get("/api/v1/runs")

    assert empty.status_code == 200
    assert empty.json() == {"items": [], "limit": 20, "total": 0}
    assert created.status_code == 200
    assert listing.status_code == 200
    body = listing.json()
    assert body["limit"] == 20
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert "cases" not in body["items"][0]
    assert set(body["items"][0]) == {
        "run_id",
        "created_at",
        "passed",
        "total",
        "passed_count",
        "failed_count",
        "skipped_count",
        "duration_ms",
    }


def test_list_obeys_limit_and_newest_first(isolated_client) -> None:
    client, _store = isolated_client
    first = client.post("/api/v1/runs", json=VALID_PAYLOAD).json()
    second = client.post("/api/v1/runs", json=VALID_PAYLOAD).json()

    response = client.get("/api/v1/runs", params={"limit": 1})

    assert response.status_code == 200
    assert response.json()["limit"] == 1
    assert response.json()["total"] == 2
    assert [item["run_id"] for item in response.json()["items"]] == [
        second["run_id"]
    ]
    assert first["run_id"] != second["run_id"]


@pytest.mark.parametrize("limit", [0, -1, 101, "not-an-integer"])
def test_list_rejects_out_of_bounds_or_invalid_limits(
    isolated_client,
    limit,
) -> None:
    client, _store = isolated_client

    response = client.get("/api/v1/runs", params={"limit": limit})

    assert response.status_code == 422


def test_detail_returns_the_complete_persisted_safe_result(
    isolated_client,
) -> None:
    client, _store = isolated_client
    created = client.post("/api/v1/runs", json=VALID_PAYLOAD)

    detail = client.get(f"/api/v1/runs/{created.json()['run_id']}")

    assert detail.status_code == 200
    assert detail.json() == created.json()
    assert "cases" in detail.json()


@pytest.mark.parametrize(
    "run_id",
    [
        "not-a-uuid",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
    ],
)
def test_missing_malformed_and_non_v4_ids_share_stable_404(
    isolated_client,
    run_id: str,
) -> None:
    client, _store = isolated_client

    response = client.get(f"/api/v1/runs/{run_id}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_post_write_failure_returns_sanitized_stable_503(
    isolated_client,
) -> None:
    client, _store = isolated_client
    failing = FailingStore()
    app.dependency_overrides[get_run_history_store] = lambda: failing

    response = client.post("/api/v1/runs", json=VALID_PAYLOAD)

    serialized = response.text
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HISTORY_PERSISTENCE_FAILED"
    assert "sqlite" not in serialized.lower()
    assert "C:\\secret" not in serialized
    assert failing.save_calls == 1


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        (
            "/api/v1/runs",
            503,
            "HISTORY_STORAGE_UNAVAILABLE",
        ),
        (
            "/api/v1/runs/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            503,
            "HISTORY_STORAGE_UNAVAILABLE",
        ),
        (
            "/api/v1/runs/not-a-uuid",
            404,
            "RUN_NOT_FOUND",
        ),
    ],
)
def test_history_read_failure_returns_sanitized_stable_503_only_after_lookup(
    isolated_client,
    path: str,
    expected_status: int,
    expected_code: str,
) -> None:
    client, _store = isolated_client
    failing = FailingStore()
    app.dependency_overrides[get_run_history_store] = lambda: failing

    response = client.get(path)

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    if expected_status == 503:
        assert "sqlite" not in response.text.lower()
        assert "C:\\secret" not in response.text


def test_corrupt_persisted_detail_returns_storage_unavailable(
    isolated_client,
) -> None:
    client, store = isolated_client
    created = client.post("/api/v1/runs", json=VALID_PAYLOAD).json()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE test_runs SET result_json = ? WHERE run_id = ?",
            ("{broken-json", created["run_id"]),
        )

    response = client.get(f"/api/v1/runs/{created['run_id']}")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "HISTORY_STORAGE_UNAVAILABLE"
    assert "broken-json" not in response.text


def test_secret_request_values_are_absent_from_database_bytes(
    isolated_client,
) -> None:
    client, store = isolated_client
    secret = VALID_PAYLOAD["variables"]["token"]

    response = client.post("/api/v1/runs", json=VALID_PAYLOAD)

    assert response.status_code == 200
    database_bytes = b"".join(
        path.read_bytes()
        for path in store.database_path.parent.glob(
            f"{store.database_path.name}*"
        )
    )
    assert secret.encode() not in database_bytes
    assert b"https://example.test" not in database_bytes
