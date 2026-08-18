from __future__ import annotations

from pathlib import Path
from threading import Barrier
from typing import Callable
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.main import app, get_run_capacity_limiter, get_run_history_store
from app.services.concurrency import RunCapacityLimiter
from app.services.run_history import HistoryStorageError, RunHistoryStore

from tests.test_run_history_storage import make_result


VALID_PAYLOAD = {
    "base_url": "https://example.test",
    "cases": [
        {
            "id": "capacity_case",
            "name": "capacity case",
            "method": "GET",
            "path": "/health",
            "assertions": [{"type": "status_code", "expected": 200}],
        }
    ],
}


class CountingStore:
    def __init__(self, result_factory: Callable = make_result) -> None:
        self.result_factory = result_factory
        self.save_calls = 0

    def save(self, _result) -> None:
        self.save_calls += 1


class CountingExecutor:
    calls = 0
    should_fail = False

    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, *_args, **_kwargs):
        type(self).calls += 1
        if type(self).should_fail:
            raise RuntimeError("synthetic executor failure")
        return make_result()


class FailingStore:
    def save(self, _result) -> None:
        raise HistoryStorageError("synthetic persistence failure")


def _client(
    tmp_path: Path,
    monkeypatch,
    limiter: RunCapacityLimiter,
    store,
) -> TestClient:
    app.dependency_overrides[get_run_capacity_limiter] = lambda: limiter
    app.dependency_overrides[get_run_history_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(
        allowed_target_origins="https://example.test",
        allow_local_targets=False,
    )
    monkeypatch.setattr(main_module, "TestExecutor", CountingExecutor)
    return TestClient(app, raise_server_exceptions=False)


def test_full_capacity_returns_429_before_executor_or_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    CountingExecutor.calls = 0
    store = CountingStore()
    limiter = RunCapacityLimiter(1)
    assert limiter.try_acquire() is True

    try:
        with _client(tmp_path, monkeypatch, limiter, store) as client:
            response = client.post("/api/v1/runs", json=VALID_PAYLOAD)
    finally:
        limiter.release()
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "RUN_CAPACITY_EXCEEDED"
    assert int(response.headers["retry-after"]) > 0
    assert CountingExecutor.calls == 0
    assert store.save_calls == 0


def test_executor_failure_releases_capacity_for_the_next_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    CountingExecutor.calls = 0
    CountingExecutor.should_fail = True
    limiter = RunCapacityLimiter(1)
    store = CountingStore()

    try:
        with _client(tmp_path, monkeypatch, limiter, store) as client:
            failed = client.post("/api/v1/runs", json=VALID_PAYLOAD)
            CountingExecutor.should_fail = False
            succeeded = client.post("/api/v1/runs", json=VALID_PAYLOAD)
    finally:
        CountingExecutor.should_fail = False
        app.dependency_overrides.clear()

    assert failed.status_code == 500
    assert succeeded.status_code == 200
    assert store.save_calls == 1


def test_persistence_failure_releases_capacity_for_the_next_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    CountingExecutor.calls = 0
    limiter = RunCapacityLimiter(1)
    failing_store = FailingStore()
    successful_store = CountingStore()

    try:
        with _client(tmp_path, monkeypatch, limiter, failing_store) as client:
            failed = client.post("/api/v1/runs", json=VALID_PAYLOAD)
            app.dependency_overrides[get_run_history_store] = (
                lambda: successful_store
            )
            succeeded = client.post("/api/v1/runs", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "HISTORY_PERSISTENCE_FAILED"
    assert succeeded.status_code == 200
    assert successful_store.save_calls == 1


def test_two_clients_overlap_and_persist_independent_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    barrier = Barrier(2)

    class ConcurrentExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            barrier.wait(timeout=5)
            return make_result()

    store = RunHistoryStore(tmp_path / "concurrent-api.sqlite3")
    limiter = RunCapacityLimiter(2)
    app.dependency_overrides[get_run_capacity_limiter] = lambda: limiter
    app.dependency_overrides[get_run_history_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(
        allowed_target_origins="https://example.test",
        allow_local_targets=False,
    )
    monkeypatch.setattr(main_module, "TestExecutor", ConcurrentExecutor)

    def submit() -> dict:
        with TestClient(app) as client:
            return client.post(
                "/api/v1/runs",
                json=VALID_PAYLOAD,
            ).json()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _item: submit(), range(2)))
    finally:
        app.dependency_overrides.clear()

    assert len({response["run_id"] for response in responses}) == 2
    items, total = store.list(limit=10)
    assert total == 2
    assert {str(item.run_id) for item in items} == {
        response["run_id"] for response in responses
    }
