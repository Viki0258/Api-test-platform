from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app import schemas


def test_settings_accepts_supported_redis_urls_without_leaking_credentials() -> None:
    for redis_url in (
        "redis://redis.example.test:6379/0",
        "rediss://synthetic-user:synthetic-password@redis.example.test/1",
    ):
        settings = Settings(_env_file=None, redis_url=redis_url)
        assert settings.redis_url == redis_url

    assert Settings(_env_file=None, redis_url="").redis_url is None

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            redis_url="http://synthetic-password@redis.example.test/0",
        )

    assert "synthetic-password" not in str(exc_info.value)


def test_async_settings_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.async_stream_name
    assert settings.async_consumer_group
    assert 1 <= settings.async_max_active_runs <= 1024
    assert 1 <= settings.async_worker_concurrency <= 64
    assert 5 <= settings.async_job_lease_seconds <= 3600
    assert 60 <= settings.async_payload_ttl_seconds <= 86400
    assert 1 <= settings.async_max_attempts <= 10


def test_async_run_accepted_serializes_uuidv4_and_queued_status() -> None:
    run_id = uuid4()
    accepted = schemas.AsyncRunAccepted(
        run_id=run_id,
        status=schemas.RunJobState.QUEUED,
        status_url=f"/api/v1/runs/{run_id}/status",
        created_at=datetime.now(timezone.utc),
    )

    assert accepted.run_id == run_id
    assert accepted.run_id.version == 4
    assert accepted.status is schemas.RunJobState.QUEUED
    assert accepted.model_dump(mode="json")["status"] == "queued"

    with pytest.raises(ValidationError):
        schemas.AsyncRunAccepted(
            run_id=run_id,
            status=schemas.RunJobState.RUNNING,
            status_url=f"/api/v1/runs/{run_id}/status",
            created_at=datetime.now(timezone.utc),
        )


def test_async_run_status_rejects_arbitrary_lifecycle_states() -> None:
    with pytest.raises(ValidationError):
        schemas.AsyncRunStatus(
            run_id=uuid4(),
            status="pending",
            attempt=0,
            created_at=datetime.now(timezone.utc),
        )


def test_async_run_status_contains_only_safe_lifecycle_fields() -> None:
    status = schemas.AsyncRunStatus(
        run_id=uuid4(),
        status=schemas.RunJobState.FAILED,
        attempt=2,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        error_code="ASYNC_RUN_FAILED",
        error_message="execution failed",
    )

    assert set(status.model_dump().keys()) == {
        "run_id",
        "status",
        "attempt",
        "created_at",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
    }
