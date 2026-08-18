from __future__ import annotations

import app.worker as worker_module


class FakeSettings:
    redis_url = "rediss://synthetic-user:synthetic-password@redis.example.test/0"
    async_stream_name = "synthetic-runs"
    async_consumer_group = "synthetic-workers"
    database_url = "mysql+pymysql://synthetic-user:synthetic-password@db/app"


def test_worker_entrypoint_builds_worker_without_starting_fastapi(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeQueue:
        def __init__(self, *args) -> None:
            captured["queue_args"] = args

    class FakeStore:
        def __init__(self, *args, **kwargs) -> None:
            captured.setdefault("stores", []).append((args, kwargs))

    class FakeWorker:
        def __init__(self, *args, **kwargs) -> None:
            captured["worker_args"] = args
            captured["worker_kwargs"] = kwargs

        def run_forever(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(worker_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(worker_module, "RedisRunQueue", FakeQueue)
    monkeypatch.setattr(worker_module, "RunQueueStore", FakeStore)
    monkeypatch.setattr(worker_module, "RunHistoryStore", FakeStore)
    monkeypatch.setattr(worker_module, "AsyncRunWorker", FakeWorker)

    worker_module.main()

    assert captured["queue_args"] == (
        FakeSettings.redis_url,
        FakeSettings.async_stream_name,
        FakeSettings.async_consumer_group,
    )
    assert len(captured["stores"]) == 2
    assert captured["worker_kwargs"] == {}
    assert captured["started"] is True
