from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import fakeredis
import pytest
import redis
from redis.exceptions import ResponseError

import app.main as main_module
from app.config import Settings
from app.main import app
from app.services.async_run_service import AsyncRunService, AsyncRunServiceError
from app.services.redis_run_queue import (
    AsyncRunQueueMessageError,
    AsyncRunQueueUnavailable,
    RedisRunQueue,
)
from app.services.run_queue_store import (
    RunQueueStorageError,
    RunQueueStore,
)


def make_redis_queue() -> RedisRunQueue:
    return RedisRunQueue(
        "redis://redis.example.test:6379/0",
        "edge-runs",
        "edge-workers",
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )


def test_redis_lease_release_and_expiry_are_idempotent() -> None:
    queue = make_redis_queue()
    run_id = uuid4()

    lease = queue.acquire_capacity("worker-a", run_id, 1, 60)
    assert lease is not None
    assert lease.release() is True
    assert lease.release() is False
    assert lease.renew() is False

    expired = queue.acquire_capacity("worker-a", run_id, 1, 0)
    assert expired is not None
    assert expired.renew() is False
    assert expired.release() is False


def test_redis_validates_arguments_and_stream_contract() -> None:
    with pytest.raises(ValueError):
        RedisRunQueue(
            "redis://redis.example.test:6379/0",
            "",
            "workers",
            redis_client=fakeredis.FakeRedis(decode_responses=True),
        )
    queue = make_redis_queue()
    with pytest.raises(ValueError):
        queue.put_payload("", "{}", 60)
    with pytest.raises(ValueError):
        queue.put_payload("key", "{}", 0)
    with pytest.raises(ValueError):
        queue.publish(uuid4(), -1, 0)
    with pytest.raises(ValueError):
        queue.read("worker", 0, 1)
    with pytest.raises(ValueError):
        queue.claim_expired("worker", -1, 1)
    with pytest.raises(ValueError):
        queue.acquire_capacity("worker", uuid4(), 0, 1)

    queue.ensure_group()
    queue.redis.xadd("edge-runs", {"run_id": str(uuid4()), "extra": "nope"})
    with pytest.raises(AsyncRunQueueMessageError):
        queue.read("worker", 1, 1)


def test_redis_driver_failures_are_wrapped_without_details() -> None:
    queue = make_redis_queue()
    with patch.object(queue.redis, "ping", side_effect=redis.ConnectionError("secret")):
        with pytest.raises(AsyncRunQueueUnavailable) as exc_info:
            queue.ping()
    assert str(exc_info.value) == "async queue is unavailable"

    with patch.object(queue.redis, "xgroup_create", side_effect=ResponseError("ERR broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.ensure_group()

    with patch.object(queue.redis, "xgroup_create", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.ensure_group()

    with patch.object(queue.redis, "set", return_value=False):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.put_payload("key", "{}", 60)

    with patch.object(queue.redis, "set", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.put_payload("key", "{}", 60)

    with patch.object(queue.redis, "get", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.get_payload("key")
    with patch.object(queue.redis, "delete", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.delete_payload("key")
    with patch.object(queue.redis, "xadd", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.publish(uuid4(), 0, 0)

    queue.ensure_group()
    with patch.object(queue.redis, "xreadgroup", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.read("worker", 1, 1)
    with patch.object(queue.redis, "xautoclaim", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.claim_expired("worker", 1, 1)
    with patch.object(queue.redis, "xack", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.ack("1-0")
    with patch.object(queue, "_acquire_capacity_script", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue.acquire_capacity("worker", uuid4(), 1, 60)
    with patch.object(queue, "_renew_capacity_script", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue._renew_capacity("worker:run", 60)
    with patch.object(queue.redis, "zrem", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue._release_capacity("worker:run")
    with patch.object(queue.redis, "time", side_effect=redis.ConnectionError("broken")):
        with pytest.raises(AsyncRunQueueUnavailable):
            queue._time_ms()

    with pytest.raises(ValueError):
        queue.publish("not-a-uuid", 0, 0)
    with pytest.raises(ValueError):
        queue.acquire_capacity("", uuid4(), 1, 60)

    queue._acquire_capacity_script = None
    with pytest.raises(AsyncRunQueueUnavailable):
        queue.ping()

    with patch.object(redis.Redis, "from_url", side_effect=ValueError("secret")):
        invalid = RedisRunQueue(
            "redis://redis.example.test:6379/0",
            "edge-runs",
            "edge-workers",
        )
    with pytest.raises(AsyncRunQueueUnavailable):
        invalid.ping()


def test_queue_store_supports_memory_and_mysql_dialects_without_connecting(
    tmp_path: Path,
) -> None:
    memory = RunQueueStore("sqlite:///:memory:")
    run_id = uuid4()
    memory.create_job(run_id, f"payload:{run_id}")
    assert memory.get_status(run_id) is not None

    mysql_store = RunQueueStore(
        "mysql+pymysql://synthetic-user:synthetic-password@db/app"
    )
    assert mysql_store.engine.dialect.name == "mysql"
    with pytest.raises(ValueError):
        RunQueueStore("postgresql://db/app")


def test_queue_store_validates_inputs_and_outbox_failure_state(tmp_path: Path) -> None:
    store = RunQueueStore(
        database_url=f"sqlite:///{tmp_path / 'edges.sqlite3'}"
    )
    run_id = uuid4()
    with pytest.raises(ValueError):
        store.create_job(run_id, "")
    store.create_job(run_id, f"payload:{run_id}")
    with pytest.raises(RunQueueStorageError):
        store.create_job(run_id, f"payload:{run_id}:duplicate")
    assert store.pending_outbox(0) == []
    assert store.mark_outbox_published(uuid4()) is False

    pending = store.pending_outbox(10)[0]
    assert store.record_outbox_failure(
        pending.outbox_id,
        "driver details!",
        "driver\nmessage",
        datetime.now(timezone.utc),
    ) is True
    retried = store.pending_outbox(10)[0]
    assert retried.publish_attempts == 1
    assert retried.error_code == "DRIVER_DETAILS"
    assert retried.error_message == "driver message"


def test_queue_store_covers_default_and_token_validation_paths() -> None:
    from app.services import run_queue_store as module

    default_store = module.RunQueueStore()
    assert default_store.database_path is not None
    memory_store = module.RunQueueStore("sqlite:///:memory:")
    with pytest.raises(ValueError):
        memory_store.claim_job(uuid4(), "worker", -1)
    with pytest.raises(ValueError):
        memory_store.claim_job(uuid4(), "worker", 1, state_version=-1)
    with pytest.raises(ValueError):
        memory_store.renew_job(uuid4(), "worker", -1)
    with pytest.raises(ValueError):
        module._normalize_run_id("not-a-uuid")
    with pytest.raises(ValueError):
        module._normalize_owner(" ")
    with pytest.raises(ValueError):
        module._encode_timestamp(datetime.now())
    assert module._state_version(module.JobToken(uuid4(), "worker", 4, 1)) == 4
    assert module._state_version(None) is None
    assert module._decode_timestamp(datetime(2026, 1, 1)) is not None


def test_async_service_wraps_payload_and_status_failures() -> None:
    class Queue:
        def __init__(self, *, ping_result=True, put_error=False, delete_error=False):
            self.ping_result = ping_result
            self.put_error = put_error
            self.delete_error = delete_error

        def ping(self):
            return self.ping_result

        def ensure_group(self):
            return True

        def put_payload(self, *_args):
            if self.put_error:
                raise AsyncRunQueueUnavailable("hidden")
            return True

        def delete_payload(self, *_args):
            if self.delete_error:
                raise AsyncRunQueueUnavailable("hidden")
            return True

    class Store:
        def create_job(self, *_args):
            raise RunQueueStorageError("hidden")

        def get_status(self, *_args):
            raise RunQueueStorageError("hidden")

    from app.schemas import TestRunRequest as RunRequest

    settings = Settings(
        _env_file=None,
        allowed_target_origins="https://example.test",
    )
    payload = RunRequest.model_validate(
        {
            "base_url": "https://example.test",
            "cases": [
                {
                    "name": "health",
                    "method": "GET",
                    "path": "/health",
                    "assertions": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    )
    service = AsyncRunService(settings, Queue(put_error=True), Store())
    with pytest.raises(AsyncRunServiceError):
        service.submit(payload)
    service = AsyncRunService(
        settings,
        Queue(put_error=True, delete_error=True),
        Store(),
    )
    with pytest.raises(AsyncRunServiceError):
        service.submit(payload)
    service = AsyncRunService(settings, Queue(ping_result=False), Store())
    with pytest.raises(AsyncRunServiceError):
        service.submit(payload)
    with pytest.raises(AsyncRunServiceError):
        AsyncRunService(settings, Queue(), Store()).status(uuid4())


def test_queue_store_state_guards_and_corrupt_database_are_safe(
    tmp_path: Path,
) -> None:
    store = RunQueueStore(
        database_url=f"sqlite:///{tmp_path / 'guards.sqlite3'}"
    )
    run_id = uuid4()
    store.create_job(run_id, f"payload:{run_id}")
    assert store.claim_job(run_id, "worker", 60, state_version=9) is None
    claim = store.claim_job(run_id, "worker", 60)
    assert claim is not None
    with pytest.raises(ValueError):
        store.renew_job(run_id, "worker", 60, state_version=-1)
    assert store.complete_job(run_id, "worker", state_version=claim.state_version) is True
    assert store.complete_job(run_id, "worker", state_version=claim.state_version) is False
    assert store.fail_job(run_id, "worker", "late", "late", state_version=claim.state_version) is False

    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_bytes(b"not sqlite")
    corrupt = RunQueueStore(database_url=f"sqlite:///{corrupt_path}")
    with pytest.raises(RunQueueStorageError) as exc_info:
        corrupt.get_status(uuid4())
    assert str(corrupt_path) not in str(exc_info.value)
    assert "sqlite" not in str(exc_info.value).lower()


def test_async_dependency_factories_use_settings_without_connecting() -> None:
    settings = Settings(
        _env_file=None,
        database_url="mysql+pymysql://synthetic-user:synthetic-password@db/app",
        redis_url="rediss://synthetic-user:synthetic-password@redis.example.test/0",
    )
    queue = main_module.get_redis_run_queue(settings)
    store = main_module.get_run_queue_store(settings)
    service = main_module.get_async_run_service(settings, queue, store)

    assert queue.stream_name == settings.async_stream_name
    assert store.database_url == settings.database_url
    assert isinstance(service, AsyncRunService)
