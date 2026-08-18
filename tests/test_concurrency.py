from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.concurrency import RunCapacityLimiter


def test_settings_expose_mysql_url_and_bounded_run_capacity() -> None:
    settings = Settings(
        database_url="mysql+pymysql://db/app",
        max_concurrent_runs=4,
    )

    assert settings.database_url == "mysql+pymysql://db/app"
    assert settings.max_concurrent_runs == 4


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://db/app",
        "mysql://db/app",
        "http://db/app",
    ],
)
def test_settings_reject_unsupported_database_urls(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=value)


@pytest.mark.parametrize("value", [0, -1, 65])
def test_settings_reject_unbounded_run_capacity(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(max_concurrent_runs=value)


def test_capacity_limiter_rejects_until_a_slot_is_released() -> None:
    limiter = RunCapacityLimiter(1)

    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False

    limiter.release()

    assert limiter.try_acquire() is True


def test_capacity_limiter_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        RunCapacityLimiter(0)
