from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import sleep
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import update

from app.config import Settings
from app.schemas import CaseStatus, TestRunRequest as RunRequest
from app.services.redis_run_queue import RedisRunQueue
from app.services.run_history import RunHistoryStore
from app.services.run_queue_store import RUN_JOBS, RunQueueStore
from tests.test_run_history_storage import make_result


VALID_REQUEST = {
    "base_url": "https://example.test",
    "variables": {"secret_token": "synthetic-secret"},
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


def worker_module():
    return importlib.import_module("app.services.async_run_worker")


def make_worker(
    tmp_path: Path,
    executor_factory,
    *,
    max_attempts: int = 2,
    max_active_runs: int = 4,
    worker_concurrency: int = 2,
    run_budget_seconds: float = 30.0,
    request_timeout_seconds: float = 10.0,
    async_job_lease_seconds: int = 5,
):
    module = worker_module()
    database_url = f"sqlite:///{tmp_path / 'worker.sqlite3'}"
    queue = RedisRunQueue(
        "redis://redis.example.test:6379/0",
        "test-runs",
        "test-workers",
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    store = RunQueueStore(database_url=database_url)
    history = RunHistoryStore(database_url=database_url)
    settings = Settings(
        _env_file=None,
        allowed_target_origins="https://example.test",
        redis_url="redis://redis.example.test:6379/0",
        async_max_attempts=max_attempts,
        async_max_active_runs=max_active_runs,
        async_worker_concurrency=worker_concurrency,
        async_job_lease_seconds=async_job_lease_seconds,
        async_payload_ttl_seconds=60,
        run_budget_seconds=run_budget_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )
    worker = module.AsyncRunWorker(
        settings,
        queue,
        store,
        history,
        executor_factory=executor_factory,
    )
    return worker, queue, store, history


def seed_job(queue, store, *, payload: str | None = None):
    run_id = uuid4()
    payload_key = f"async-run:{run_id}:payload"
    if payload is not None:
        queue.put_payload(payload_key, payload, ttl_seconds=60)
    store.create_job(run_id, payload_key)
    return run_id


class SuccessfulExecutor:
    calls = 0

    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, *_args, **_kwargs):
        type(self).calls += 1
        return make_result(passed=True)


class FailedResultExecutor:
    calls = 0

    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, *_args, **_kwargs):
        type(self).calls += 1
        return make_result(passed=False)


def test_worker_dispatches_executes_saves_redacted_result_and_cleans_payload(
    tmp_path: Path,
) -> None:
    SuccessfulExecutor.calls = 0
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    assert worker.run_once() is True

    record = store.get_status(run_id)
    assert record is not None
    assert record.state.value == "completed"
    assert SuccessfulExecutor.calls == 1
    assert history.get(run_id) is not None
    assert queue.get_payload(record.payload_key) is None
    assert store.pending_outbox(limit=10) == []


def test_worker_uses_effective_execution_lease_for_redis_capacity(
    tmp_path: Path,
) -> None:
    worker, queue, store, _history = make_worker(
        tmp_path,
        SuccessfulExecutor,
        run_budget_seconds=20,
        request_timeout_seconds=10,
        async_job_lease_seconds=5,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    calls: list[tuple[str, object, int, int]] = []
    original_acquire = queue.acquire_capacity

    def capture_acquire(owner_id, captured_run_id, limit, lease_seconds):
        calls.append((owner_id, captured_run_id, limit, lease_seconds))
        return original_acquire(
            owner_id,
            captured_run_id,
            limit,
            lease_seconds,
        )

    queue.acquire_capacity = capture_acquire

    assert worker.run_once() is True

    assert calls
    assert calls[0][3] == worker._lease_seconds
    assert calls[0][3] >= 20 + 10 + 2


def test_dispatcher_republishes_same_outbox_id_idempotently(
    tmp_path: Path,
) -> None:
    worker, queue, store, _history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)
    outbox_id = store.pending_outbox(limit=1)[0].outbox_id

    published_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_publish = queue.publish

    def capture_publish(*args, **kwargs):
        published_calls.append((args, kwargs))
        return original_publish(*args, **kwargs)

    queue.publish = capture_publish
    original_mark = store.mark_outbox_published
    mark_calls = 0

    def leave_pending_once(captured_outbox_id):
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 1:
            return False
        return original_mark(captured_outbox_id)

    store.mark_outbox_published = leave_pending_once

    worker._dispatch_outbox()
    worker._dispatch_outbox()

    assert published_calls[0][1]["outbox_id"] == outbox_id
    assert published_calls[1][1]["outbox_id"] == outbox_id
    assert len(queue.redis.xrange(queue.stream_name)) == 1
    assert store.pending_outbox(limit=10) == []
    assert run_id


def test_assertion_failure_is_completed_and_persisted_as_failed_result(
    tmp_path: Path,
) -> None:
    FailedResultExecutor.calls = 0
    worker, queue, store, history = make_worker(tmp_path, FailedResultExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    assert worker.run_once() is True

    record = store.get_status(run_id)
    result = history.get(run_id)
    assert record.state.value == "completed"
    assert result is not None
    assert result.passed is False
    assert FailedResultExecutor.calls == 1


@pytest.mark.parametrize(
    "error_code",
    ["REQUEST_TIMEOUT", "CONNECT_FAILED", "NETWORK_ERROR"],
)
def test_infrastructure_case_result_is_retried_and_then_failed(
    tmp_path: Path,
    error_code: str,
) -> None:
    class InfrastructureResultExecutor:
        calls = 0

        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            type(self).calls += 1
            case = make_result().cases[0].model_copy(
                update={
                    "status": CaseStatus.FAILED,
                    "passed": False,
                    "error_code": error_code,
                }
            )
            return make_result(passed=False).model_copy(update={"cases": [case]})

    worker, queue, store, history = make_worker(
        tmp_path,
        InfrastructureResultExecutor,
        max_attempts=2,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    assert worker.run_once() is True
    first = store.get_status(run_id)
    assert first is not None
    assert first.state.value == "queued"
    assert first.attempt == 1
    assert history.get(run_id) is None

    sleep(1.1)
    assert worker.run_once() is True
    final = store.get_status(run_id)
    assert final is not None
    assert final.state.value == "failed"
    assert final.error_code == "ASYNC_EXECUTION_FAILED"
    assert history.get(run_id) is None
    assert InfrastructureResultExecutor.calls == 2


def test_worker_does_not_persist_after_lease_loss(
    tmp_path: Path,
) -> None:
    SuccessfulExecutor.calls = 0
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    lease_lost = Event()
    lease_lost.set()

    class NoopThread:
        def join(self, timeout: float | None = None) -> None:
            return None

    def lost_heartbeat(*_args, **_kwargs):
        return Event(), lease_lost, NoopThread()

    worker._start_heartbeat = lost_heartbeat

    assert worker.run_once() is False
    record = store.get_status(run_id)
    assert record is not None
    assert record.state.value == "running"
    assert history.get(run_id) is None
    assert SuccessfulExecutor.calls == 0


def test_history_is_not_written_when_database_lease_expires_after_result(
    tmp_path: Path,
) -> None:
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    class LeaseExpiringExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            expired_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            with store.engine.begin() as connection:
                connection.execute(
                    update(RUN_JOBS)
                    .where(RUN_JOBS.c.run_id == str(run_id))
                    .values(lease_expires_at=expired_at)
                )
            return make_result()

    worker.executor_factory = LeaseExpiringExecutor

    assert worker.run_once() is False
    record = store.get_status(run_id)
    assert record is not None
    assert record.state.value == "running"
    assert history.get(run_id) is None


def test_heartbeat_and_terminal_update_use_current_owner_and_version(
    tmp_path: Path,
) -> None:
    started = Event()
    heartbeat_seen = Event()
    release = Event()
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    worker._lease_seconds = 3
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    renew_calls: list[tuple[object, ...]] = []
    original_renew = store.renew_job

    def capture_renew(*args, **kwargs):
        renew_calls.append((*args, kwargs))
        heartbeat_seen.set()
        return original_renew(*args, **kwargs)

    store.renew_job = capture_renew
    complete_calls: list[tuple[object, ...]] = []
    original_complete = store.complete_job

    def capture_complete(*args, **kwargs):
        complete_calls.append((*args, kwargs))
        return original_complete(*args, **kwargs)

    store.complete_job = capture_complete

    class HeartbeatExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            started.set()
            assert heartbeat_seen.wait(timeout=4)
            release.set()
            return make_result()

    worker.executor_factory = HeartbeatExecutor

    assert worker.run_once() is True
    assert started.is_set()
    assert renew_calls
    assert all(call[1] == worker.worker_id for call in renew_calls)
    assert all(call[2] == worker._lease_seconds for call in renew_calls)
    assert all(call[3]["state_version"] == 1 for call in renew_calls)
    assert complete_calls
    assert complete_calls[0][0] == run_id
    assert complete_calls[0][1] == worker.worker_id
    assert complete_calls[0][2]["state_version"] == 1
    assert store.get_status(run_id).state.value == "completed"
    assert history.get(run_id) is not None


def test_infrastructure_failure_retries_then_enters_failed_state(
    tmp_path: Path,
) -> None:
    class FailingExecutor:
        calls = 0

        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            type(self).calls += 1
            raise RuntimeError("driver details must not escape")

    worker, queue, store, _history = make_worker(
        tmp_path,
        FailingExecutor,
        max_attempts=2,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    assert worker.run_once() is True
    queued = store.get_status(run_id)
    assert queued.state.value == "queued"
    assert queued.attempt == 1

    sleep(1.1)
    assert worker.run_once() is True
    failed = store.get_status(run_id)
    assert failed.state.value == "failed"
    assert failed.error_code == "ASYNC_EXECUTION_FAILED"
    assert "driver details" not in (failed.error_message or "")
    assert FailingExecutor.calls == 2


def test_duplicate_delivery_does_not_execute_or_save_twice(
    tmp_path: Path,
) -> None:
    SuccessfulExecutor.calls = 0
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    assert worker.run_once() is True
    record = store.get_status(run_id)
    duplicate_id = queue.publish(run_id, attempt=0, state_version=0)
    assert worker.run_once() is True

    assert SuccessfulExecutor.calls == 1
    assert history.get(run_id) is not None
    assert queue.ack(duplicate_id) is False
    assert record.state.value == "completed"


def test_missing_payload_is_sanitized_terminal_failure(
    tmp_path: Path,
) -> None:
    SuccessfulExecutor.calls = 0
    worker, queue, store, _history = make_worker(tmp_path, SuccessfulExecutor)
    run_id = seed_job(queue, store, payload=None)

    assert worker.run_once() is True

    record = store.get_status(run_id)
    assert record.state.value == "failed"
    assert record.error_code == "ASYNC_PAYLOAD_MISSING"
    assert "payload" in (record.error_message or "")
    assert SuccessfulExecutor.calls == 0


def test_global_capacity_prevents_second_worker_from_starting_execution(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    class BlockingExecutor:
        calls = 0

        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            type(self).calls += 1
            started.set()
            release.wait(timeout=5)
            return make_result()

    worker, queue, store, _history = make_worker(
        tmp_path,
        BlockingExecutor,
        max_active_runs=1,
        worker_concurrency=2,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    seed_job(queue, store, payload=payload)
    seed_job(queue, store, payload=payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker.run_once) for _ in range(2)]
        assert started.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert BlockingExecutor.calls == 1
    assert any(results)


def test_worker_local_slot_and_graceful_forever_shutdown(
    tmp_path: Path,
) -> None:
    worker, _queue, _store, _history = make_worker(
        tmp_path,
        SuccessfulExecutor,
        worker_concurrency=1,
    )
    assert worker._local_slots.acquire(blocking=False) is True
    try:
        assert worker.run_once() is False
    finally:
        worker._local_slots.release()

    stop = Event()
    calls: list[bool] = []

    def stop_once() -> bool:
        calls.append(True)
        stop.set()
        return False

    worker.run_once = stop_once
    worker.run_forever(stop)
    assert calls


def test_invalid_payload_and_disallowed_target_are_terminal_failures(
    tmp_path: Path,
) -> None:
    worker, queue, store, _history = make_worker(tmp_path, SuccessfulExecutor)
    invalid_id = seed_job(queue, store, payload="not-json")
    disallowed_payload = RunRequest(
        base_url="https://other.example.test",
        cases=RunRequest.model_validate(VALID_REQUEST).cases,
    ).model_dump_json()
    disallowed_id = seed_job(queue, store, payload=disallowed_payload)

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert store.get_status(invalid_id).error_code == "ASYNC_PAYLOAD_INVALID"
    assert store.get_status(disallowed_id).error_code == "ASYNC_TARGET_NOT_ALLOWED"


def test_existing_history_skips_duplicate_executor_call(tmp_path: Path) -> None:
    SuccessfulExecutor.calls = 0
    worker, queue, store, history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)
    history.save(make_result().model_copy(update={"run_id": run_id}))

    assert worker.run_once() is True
    assert store.get_status(run_id).state.value == "completed"
    assert SuccessfulExecutor.calls == 0


def test_worker_handles_outbox_and_queue_error_paths(tmp_path: Path) -> None:
    worker, queue, store, _history = make_worker(tmp_path, SuccessfulExecutor)
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    seed_job(queue, store, payload=payload)
    module = worker_module()

    original_publish = queue.publish
    queue.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        module.AsyncRunQueueUnavailable("async queue is unavailable")
    )
    worker._dispatch_outbox()
    queue.publish = original_publish
    worker._dispatch_outbox()

    original_pending = store.pending_outbox
    store.pending_outbox = lambda *args, **kwargs: (_ for _ in ()).throw(
        module.RunQueueStorageError("storage unavailable")
    )
    worker._dispatch_outbox()
    store.pending_outbox = original_pending

    original_claim = queue.claim_expired
    queue.claim_expired = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        module.AsyncRunQueueUnavailable("async queue is unavailable")
    )
    assert worker._next_message() is None
    queue.claim_expired = original_claim

    queue.acquire_capacity = lambda *_args, **_kwargs: None
    assert worker.run_once() is False


def test_worker_acknowledges_malformed_stream_message(tmp_path: Path) -> None:
    worker, queue, _store, _history = make_worker(
        tmp_path,
        SuccessfulExecutor,
    )
    queue.ensure_group()
    message_id = queue.redis.xadd(
        "test-runs",
        {
            "run_id": str(uuid4()),
            "attempt": "-1",
            "state_version": "0",
        },
    )

    assert worker.run_once() is False
    assert queue.ack(message_id) is False


def test_worker_acknowledges_old_message_before_recovery_execution(
    tmp_path: Path,
) -> None:
    worker, queue, store, _history = make_worker(
        tmp_path,
        SuccessfulExecutor,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)
    worker._dispatch_outbox()
    queue.ensure_group()
    original = queue.read("old-worker", count=1, block_ms=1)[0]
    old_claim = store.claim_job(
        run_id,
        "old-worker",
        lease_seconds=0,
        state_version=original.state_version,
        attempt=original.attempt,
    )
    assert old_claim is not None

    worker._lease_seconds = 0
    worker._execute_claim = lambda *_args: True

    assert worker.run_once() is True
    assert queue.ack(original.message_id) is False


def test_worker_drops_result_when_lease_is_lost_after_execution(
    tmp_path: Path,
) -> None:
    lease_lost = Event()

    class LeaseLostExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self, *_args, **_kwargs):
            lease_lost.set()
            return make_result(passed=True)

    worker, queue, store, history = make_worker(
        tmp_path,
        LeaseLostExecutor,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    run_id = seed_job(queue, store, payload=payload)

    class NoopThread:
        def join(self, timeout: float | None = None) -> None:
            return None

    worker._start_heartbeat = lambda *_args, **_kwargs: (
        Event(),
        lease_lost,
        NoopThread(),
    )

    assert worker.run_once() is False
    assert store.get_status(run_id).state.value == "running"
    assert history.get(run_id) is None


def test_worker_retries_when_payload_or_history_store_is_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    module = worker_module()

    class FailingHistory:
        def get(self, _run_id):
            raise module.HistoryStorageError("history unavailable")

        def save(self, _result):
            raise AssertionError("save should not be reached")

    worker, queue, store, _history = make_worker(
        tmp_path,
        SuccessfulExecutor,
    )
    payload = RunRequest.model_validate(VALID_REQUEST).model_dump_json()
    history_failing_id = seed_job(queue, store, payload=payload)
    worker.history_store = FailingHistory()
    assert worker.run_once() is True
    assert store.get_status(history_failing_id).state.value == "queued"

    worker, queue, store, _history = make_worker(
        tmp_path / "payload",
        SuccessfulExecutor,
    )
    payload_id = seed_job(queue, store, payload=payload)
    original_get = queue.get_payload
    queue.get_payload = lambda _key: (_ for _ in ()).throw(
        module.AsyncRunQueueUnavailable("async queue is unavailable")
    )
    assert worker.run_once() is True
    queue.get_payload = original_get
    assert store.get_status(payload_id).state.value == "queued"
