from __future__ import annotations

import importlib
from pathlib import Path
from uuid import UUID

import fakeredis
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.main import app
from app.schemas import TestRunRequest as RunRequest
from app.services.run_queue_store import RunQueueStorageError, RunQueueStore


VALID_PAYLOAD = {
    "base_url": "https://example.test",
    "variables": {
        "secret_token": "synthetic-secret",
        "expected_id": 7,
    },
    "secret_variables": ["secret_token"],
    "cases": [
        {
            "id": "health",
            "name": "health",
            "method": "GET",
            "path": "/health",
            "headers": {"Authorization": "Bearer {{secret_token}}"},
            "assertions": [{"type": "status_code", "expected": 200}],
        }
    ],
}


def service_module():
    return importlib.import_module("app.services.async_run_service")


class RecordingQueue:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.ping_calls = 0
        self.ensure_group_calls = 0
        self.payloads: dict[str, str] = {}
        self.deleted: list[str] = []

    def ping(self) -> bool:
        self.ping_calls += 1
        if self.unavailable:
            raise service_module().AsyncRunQueueUnavailable(
                "async queue is unavailable"
            )
        return True

    def ensure_group(self) -> bool:
        self.ensure_group_calls += 1
        return True

    def put_payload(self, key: str, payload: str, ttl_seconds: int) -> bool:
        self.payloads[key] = payload
        return True

    def delete_payload(self, key: str) -> bool:
        self.deleted.append(key)
        self.payloads.pop(key, None)
        return True


class FailingStore:
    def create_job(self, *_args, **_kwargs):
        raise RunQueueStorageError("synthetic storage failure")


def make_service(
    tmp_path: Path,
    *,
    queue=None,
    store=None,
    settings: Settings | None = None,
):
    module = service_module()
    if queue is None:
        queue = module.RedisRunQueue(
            "redis://redis.example.test:6379/0",
            "test-runs",
            "test-workers",
            redis_client=fakeredis.FakeRedis(decode_responses=True),
        )
    if store is None:
        store = RunQueueStore(
            database_url=f"sqlite:///{tmp_path / 'async-api.sqlite3'}"
        )
    if settings is None:
        settings = Settings(
            _env_file=None,
            allowed_target_origins="https://example.test",
            redis_url="redis://redis.example.test:6379/0",
        )
    return module.AsyncRunService(settings, queue, store), queue, store


def override_service(service) -> None:
    app.dependency_overrides[
        getattr(main_module, "get_async_run_service")
    ] = lambda: service


def test_async_submission_returns_202_without_invoking_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, queue, store = make_service(tmp_path)

    class ExplodingExecutor:
        def __init__(self, **_kwargs):
            raise AssertionError("async API must not construct TestExecutor")

    monkeypatch.setattr(main_module, "TestExecutor", ExplodingExecutor)
    override_service(service)
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/runs/async", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    run_id = UUID(body["run_id"])
    assert run_id.version == 4
    assert body["status"] == "queued"
    assert body["status_url"] == f"/api/v1/runs/{run_id}/status"
    assert queue.redis.xrange("test-runs") == []
    record = store.get_status(run_id)
    assert record is not None
    assert record.state.value == "queued"
    assert queue.redis.exists(record.payload_key) == 1


def test_invalid_target_is_rejected_before_redis_or_job_creation(
    tmp_path: Path,
) -> None:
    queue = RecordingQueue()
    service, _queue, _store = make_service(
        tmp_path,
        queue=queue,
        settings=Settings(_env_file=None, allowed_target_origins=""),
    )
    override_service(service)
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/runs/async", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_NOT_ALLOWED"
    assert queue.ping_calls == 0
    assert queue.payloads == {}


def test_unavailable_redis_returns_stable_503_without_executor_or_store(
    tmp_path: Path,
) -> None:
    queue = RecordingQueue(unavailable=True)
    store = FailingStore()
    service, _queue, _store = make_service(
        tmp_path,
        queue=queue,
        store=store,
    )
    override_service(service)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/runs/async", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "ASYNC_RUNS_UNAVAILABLE",
        "message": "async run queue is temporarily unavailable",
    }
    assert "redis" not in response.text.lower()
    assert "synthetic-password" not in response.text


def test_job_creation_failure_removes_transient_payload(tmp_path: Path) -> None:
    queue = RecordingQueue()
    service, _queue, _store = make_service(
        tmp_path,
        queue=queue,
        store=FailingStore(),
    )
    override_service(service)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/runs/async", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ASYNC_RUN_STORAGE_FAILED"
    assert len(queue.deleted) == 1
    assert queue.payloads == {}


def test_status_endpoint_returns_safe_queued_and_running_states(
    tmp_path: Path,
) -> None:
    service, queue, store = make_service(tmp_path)
    accepted = service.submit(RunRequest.model_validate(VALID_PAYLOAD))
    run_id = accepted.run_id
    response_records = []

    override_service(service)
    try:
        with TestClient(app) as client:
            response_records.append(
                client.get(f"/api/v1/runs/{run_id}/status")
            )
            claim = store.claim_job(run_id, "worker-a", lease_seconds=60)
            assert claim is not None
            response_records.append(
                client.get(f"/api/v1/runs/{run_id}/status")
            )
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in response_records] == [200, 200]
    assert [response.json()["status"] for response in response_records] == [
        "queued",
        "running",
    ]
    status_text = response_records[0].text + response_records[1].text
    assert "synthetic-secret" not in status_text
    assert "example.test" not in status_text
    assert "headers" not in status_text


def test_status_endpoint_maps_missing_uuid_to_existing_not_found_shape(
    tmp_path: Path,
) -> None:
    service, _queue, _store = make_service(tmp_path)
    override_service(service)
    missing_id = UUID("12345678-1234-4234-8234-123456789abc")
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/runs/{missing_id}/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"
