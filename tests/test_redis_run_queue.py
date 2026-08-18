from __future__ import annotations

import importlib
from uuid import uuid4

import fakeredis
import pytest


def queue_module():
    return importlib.import_module("app.services.redis_run_queue")


def make_queue():
    module = queue_module()
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = module.RedisRunQueue(
        "redis://redis.example.test:6379/0",
        "test-runs",
        "test-workers",
        redis_client=client,
    )
    return queue, client


def test_consumer_group_initialization_is_idempotent() -> None:
    queue, _client = make_queue()

    queue.ensure_group()
    queue.ensure_group()


def test_payload_has_ttl_and_can_be_deleted() -> None:
    queue, client = make_queue()
    payload_key = "run-payload:synthetic"

    queue.put_payload(payload_key, '{"cases": []}', ttl_seconds=60)

    assert queue.get_payload(payload_key) == '{"cases": []}'
    assert 0 < client.ttl(payload_key) <= 60
    assert queue.delete_payload(payload_key) is True
    assert queue.get_payload(payload_key) is None
    assert queue.delete_payload(payload_key) is False


def test_stream_event_contains_only_safe_fields() -> None:
    queue, client = make_queue()
    run_id = uuid4()

    message_id = queue.publish(run_id, attempt=2, state_version=7)

    entries = client.xrange("test-runs")
    assert entries[0][0] == message_id
    assert set(entries[0][1]) == {"run_id", "attempt", "state_version"}
    assert entries[0][1]["run_id"] == str(run_id)
    assert entries[0][1]["attempt"] == "2"
    assert entries[0][1]["state_version"] == "7"


def test_republishing_an_outbox_id_returns_same_stream_message() -> None:
    queue, client = make_queue()
    run_id = uuid4()
    outbox_id = uuid4()

    first_id = queue.publish(
        run_id,
        attempt=2,
        state_version=7,
        outbox_id=outbox_id,
    )
    second_id = queue.publish(
        run_id,
        attempt=2,
        state_version=7,
        outbox_id=outbox_id,
    )

    assert second_id == first_id
    assert len(client.xrange("test-runs")) == 1


def test_global_capacity_is_bounded_and_released_across_workers() -> None:
    queue, _client = make_queue()
    first_run = uuid4()
    second_run = uuid4()

    first = queue.acquire_capacity(
        "worker-a",
        first_run,
        limit=1,
        lease_seconds=60,
    )
    assert first is not None
    assert queue.acquire_capacity(
        "worker-b",
        second_run,
        limit=1,
        lease_seconds=60,
    ) is None
    assert first.renew() is True
    assert first.release() is True
    assert queue.acquire_capacity(
        "worker-b",
        second_run,
        limit=1,
        lease_seconds=60,
    ) is not None


def test_expired_capacity_lease_is_reclaimed_atomically() -> None:
    queue, _client = make_queue()
    first_run = uuid4()
    second_run = uuid4()

    assert queue.acquire_capacity(
        "worker-a",
        first_run,
        limit=1,
        lease_seconds=0,
    ) is not None
    assert queue.acquire_capacity(
        "worker-b",
        second_run,
        limit=1,
        lease_seconds=60,
    ) is not None


def test_pending_message_can_be_read_acknowledged_and_claimed() -> None:
    queue, _client = make_queue()
    queue.ensure_group()
    first_id = queue.publish(uuid4(), attempt=0, state_version=0)

    messages = queue.read("worker-a", count=1, block_ms=1)
    assert len(messages) == 1
    assert messages[0].message_id == first_id
    assert messages[0].attempt == 0
    assert queue.ack(first_id) is True
    assert queue.ack(first_id) is False

    second_id = queue.publish(uuid4(), attempt=1, state_version=2)
    assert queue.read("worker-a", count=1, block_ms=1)[0].message_id == second_id
    claimed = queue.claim_expired("worker-b", min_idle_ms=0, count=1)
    assert [message.message_id for message in claimed] == [second_id]
    assert queue.ack(second_id) is True


def test_negative_stream_counters_are_rejected_as_message_errors() -> None:
    module = queue_module()
    queue, client = make_queue()
    queue.ensure_group()
    message_id = client.xadd(
        "test-runs",
        {
            "run_id": str(uuid4()),
            "attempt": "-1",
            "state_version": "0",
        },
    )

    with pytest.raises(module.AsyncRunQueueMessageError) as exc_info:
        queue.read("worker-a", count=1, block_ms=1)
    assert exc_info.value.message_id == message_id


def test_unavailable_queue_uses_stable_error_without_driver_details() -> None:
    module = queue_module()
    queue = module.RedisRunQueue(
        None,
        "test-runs",
        "test-workers",
    )

    with pytest.raises(module.AsyncRunQueueUnavailable) as exc_info:
        queue.ping()

    assert str(exc_info.value) == "async queue is unavailable"
