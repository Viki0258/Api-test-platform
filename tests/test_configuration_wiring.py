from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.main import app, get_run_history_store


def test_async_settings_are_available_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.test:6379/0")
    monkeypatch.setenv("ASYNC_WORKER_CONCURRENCY", "3")

    settings = Settings(_env_file=None)

    assert settings.redis_url == "redis://redis.example.test:6379/0"
    assert settings.async_worker_concurrency == 3


def test_history_dependency_receives_database_url_from_settings(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class RecordingStore:
        def __init__(self, *args, **kwargs) -> None:
            captured["args"] = args
            captured.update(kwargs)

        def list(self, _limit: int):
            return [], 0

    monkeypatch.setattr(main_module, "RunHistoryStore", RecordingStore)
    cache_clear = getattr(get_run_history_store, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="mysql+pymysql://synthetic-user:synthetic-password@db/app",
    )

    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs")
    finally:
        app.dependency_overrides.clear()
        if cache_clear is not None:
            cache_clear()

    assert response.status_code == 200
    assert captured["database_url"] == (
        "mysql+pymysql://synthetic-user:synthetic-password@db/app"
    )
