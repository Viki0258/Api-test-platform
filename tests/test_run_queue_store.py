from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


def queue_module():
    return importlib.import_module("app.services.run_queue_store")


def make_store(tmp_path):
    module = queue_module()
    return module.RunQueueStore(
        database_url=f"sqlite:///{tmp_path / 'run-queue.sqlite3'}"
    )


def test_create_job_initializes_tables_and_records_queued_state(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()

    store.create_job(run_id, f"run-payload:{run_id}")

    record = store.get_status(run_id)
    assert record is not None
    assert record.run_id == run_id
    assert record.state.value == "queued"
    assert record.attempt == 0
    assert record.payload_key == f"run-payload:{run_id}"
    assert record.lease_owner is None


def test_claim_is_conditional_and_stale_token_cannot_complete_job(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")

    first = store.claim_job(run_id, "worker-a", lease_seconds=0)
    assert first is not None
    assert first.attempt == 1
    assert store.claim_job(run_id, "worker-b", lease_seconds=60) is not None
    second = store.get_status(run_id)
    assert second is not None
    assert second.lease_owner == "worker-b"

    assert store.complete_job(
        run_id,
        "worker-a",
        state_version=first.state_version,
    ) is False
    assert store.complete_job(
        run_id,
        "worker-b",
        state_version=second.state_version,
    ) is True
    assert store.get_status(run_id).state.value == "completed"


def test_expired_recovery_publishes_a_fresh_fenced_event(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")

    first = store.claim_job(
        run_id,
        "worker-a",
        lease_seconds=0,
        state_version=0,
        attempt=0,
    )
    assert first is not None

    second = store.claim_job(
        run_id,
        "worker-b",
        lease_seconds=0,
        state_version=0,
        attempt=0,
    )
    assert second is not None
    assert second.recovered is True

    recovery = [
        item
        for item in store.pending_outbox(limit=10)
        if item.event_kind == "recovery"
    ]
    assert len(recovery) == 1
    assert recovery[0].state_version == second.state_version
    assert recovery[0].attempt == second.attempt

    third = store.claim_job(
        run_id,
        "worker-c",
        lease_seconds=60,
        state_version=recovery[0].state_version,
        attempt=recovery[0].attempt,
    )
    assert third is not None
    assert third.attempt == second.attempt + 1


def test_expired_owner_cannot_write_a_terminal_state(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")
    claim = store.claim_job(run_id, "worker-a", lease_seconds=0)
    assert claim is not None

    assert store.complete_job(
        run_id,
        "worker-a",
        state_version=claim.state_version,
    ) is False
    assert store.get_status(run_id).state.value == "running"


def test_claim_rejects_unknown_stale_future_and_terminal_jobs(tmp_path) -> None:
    store = make_store(tmp_path)

    assert store.claim_job(uuid4(), "worker", lease_seconds=60) is None

    queued_id = uuid4()
    store.create_job(queued_id, f"payload:{queued_id}")
    assert store.claim_job(
        queued_id,
        "worker",
        lease_seconds=60,
        state_version=99,
    ) is None
    assert store.claim_job(
        queued_id,
        "worker",
        lease_seconds=60,
        attempt=99,
    ) is None

    retry_id = uuid4()
    store.create_job(retry_id, f"payload:{retry_id}")
    retry_claim = store.claim_job(retry_id, "worker", lease_seconds=60)
    assert retry_claim is not None
    assert store.schedule_retry(
        retry_id,
        "worker",
        "ASYNC_RETRYABLE",
        "temporary failure",
        datetime.now(timezone.utc) + timedelta(seconds=60),
        state_version=retry_claim.state_version,
    ) is True
    assert store.claim_job(
        retry_id,
        "worker",
        lease_seconds=60,
        state_version=retry_claim.state_version + 1,
        attempt=retry_claim.attempt,
    ) is None

    running_id = uuid4()
    store.create_job(running_id, f"payload:{running_id}")
    running_claim = store.claim_job(running_id, "worker", lease_seconds=0)
    assert running_claim is not None
    assert store.claim_job(
        running_id,
        "other-worker",
        lease_seconds=0,
        attempt=99,
    ) is None
    assert store.claim_job(
        running_id,
        "other-worker",
        lease_seconds=0,
        state_version=99,
    ) is None

    terminal_id = uuid4()
    store.create_job(terminal_id, f"payload:{terminal_id}")
    terminal_claim = store.claim_job(terminal_id, "worker", lease_seconds=60)
    assert terminal_claim is not None
    assert store.complete_job(
        terminal_id,
        "worker",
        state_version=terminal_claim.state_version,
    ) is True
    assert store.claim_job(terminal_id, "other-worker", lease_seconds=60) is None


def test_renew_requires_current_owner_and_retry_returns_to_queued(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")
    claim = store.claim_job(run_id, "worker-a", lease_seconds=60)
    assert claim is not None

    assert store.renew_job(
        run_id,
        "wrong-worker",
        lease_seconds=60,
        state_version=claim.state_version,
    ) is False
    assert store.renew_job(
        run_id,
        "worker-a",
        lease_seconds=60,
        state_version=claim.state_version,
    ) is True

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert store.schedule_retry(
        run_id,
        "worker-a",
        "ASYNC_RETRYABLE",
        "temporary failure",
        retry_at,
        state_version=claim.state_version,
    ) is True
    record = store.get_status(run_id)
    assert record.state.value == "queued"
    assert record.lease_owner is None
    assert record.error_code is None


def test_outbox_is_durable_and_marking_is_idempotent(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")

    pending = store.pending_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0].run_id == run_id
    assert pending[0].attempt == 0
    assert pending[0].state_version == 0
    assert store.mark_outbox_published(pending[0].outbox_id) is True
    assert store.mark_outbox_published(pending[0].outbox_id) is False
    assert store.pending_outbox(limit=10) == []


def test_fail_job_persists_only_sanitized_terminal_error(tmp_path) -> None:
    store = make_store(tmp_path)
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")
    claim = store.claim_job(run_id, "worker-a", lease_seconds=60)
    assert claim is not None

    assert store.fail_job(
        run_id,
        "worker-a",
        "async infrastructure failure!",
        "driver details should not be returned\n",
        state_version=claim.state_version,
    ) is True
    record = store.get_status(run_id)
    assert record.state.value == "failed"
    assert record.error_code == "ASYNC_INFRASTRUCTURE_FAILURE"
    assert record.error_message == "driver details should not be returned"
