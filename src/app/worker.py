from __future__ import annotations

from app.config import get_settings
from app.services.async_run_worker import AsyncRunWorker
from app.services.redis_run_queue import RedisRunQueue
from app.services.run_history import RunHistoryStore
from app.services.run_queue_store import RunQueueStore


def main() -> None:
    settings = get_settings()
    queue = RedisRunQueue(
        settings.redis_url,
        settings.async_stream_name,
        settings.async_consumer_group,
    )
    queue_store = RunQueueStore(database_url=settings.database_url)
    history_store = RunHistoryStore(database_url=settings.database_url)
    worker = AsyncRunWorker(
        settings,
        queue,
        queue_store,
        history_store,
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
