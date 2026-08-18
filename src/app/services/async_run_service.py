from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from app.config import Settings, target_is_allowed
from app.schemas import AsyncRunAccepted, AsyncRunStatus, TestRunRequest
from app.services.redis_run_queue import (
    AsyncRunQueueUnavailable,
    RedisRunQueue,
)
from app.services.run_queue_store import (
    RunQueueStorageError,
    RunQueueStore,
)


@dataclass(frozen=True)
class AsyncRunServiceError(RuntimeError):
    status_code: int
    code: str
    message: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)


class AsyncRunService:
    def __init__(
        self,
        settings: Settings,
        queue: RedisRunQueue,
        store: RunQueueStore,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.store = store

    def submit(self, payload: TestRunRequest) -> AsyncRunAccepted:
        base_url = str(payload.base_url)
        if not target_is_allowed(base_url, self.settings):
            raise AsyncRunServiceError(
                422,
                "TARGET_NOT_ALLOWED",
                "base_url origin is not allowed",
            )

        self._ensure_queue_available()
        run_id = uuid4()
        payload_key = self.payload_key(run_id)
        try:
            self.queue.put_payload(
                payload_key,
                payload.model_dump_json(),
                self.settings.async_payload_ttl_seconds,
            )
        except AsyncRunQueueUnavailable:
            self._delete_payload_quietly(payload_key)
            raise self._queue_unavailable() from None

        try:
            record = self.store.create_job(run_id, payload_key)
        except RunQueueStorageError:
            self._delete_payload_quietly(payload_key)
            raise AsyncRunServiceError(
                503,
                "ASYNC_RUN_STORAGE_FAILED",
                "async run storage is temporarily unavailable",
            ) from None

        return AsyncRunAccepted(
            run_id=record.run_id,
            status=record.state,
            status_url=f"/api/v1/runs/{record.run_id}/status",
            created_at=record.created_at,
        )

    def status(self, run_id: UUID) -> AsyncRunStatus:
        try:
            record = self.store.get_status(run_id)
        except RunQueueStorageError:
            raise AsyncRunServiceError(
                503,
                "ASYNC_RUN_STORAGE_UNAVAILABLE",
                "async run storage is temporarily unavailable",
            ) from None
        if record is None:
            raise AsyncRunServiceError(
                404,
                "RUN_NOT_FOUND",
                "test run was not found",
            )
        return AsyncRunStatus(
            run_id=record.run_id,
            status=record.state,
            attempt=record.attempt,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error_code=record.error_code,
            error_message=record.error_message,
        )

    @staticmethod
    def payload_key(run_id: UUID) -> str:
        return f"async-run:{run_id}:payload"

    def _ensure_queue_available(self) -> None:
        try:
            if not self.queue.ping():
                raise AsyncRunQueueUnavailable(
                    "async queue is unavailable"
                )
            self.queue.ensure_group()
        except AsyncRunQueueUnavailable:
            raise self._queue_unavailable() from None

    def _delete_payload_quietly(self, payload_key: str) -> None:
        try:
            self.queue.delete_payload(payload_key)
        except AsyncRunQueueUnavailable:
            return

    @staticmethod
    def _queue_unavailable() -> AsyncRunServiceError:
        return AsyncRunServiceError(
            503,
            "ASYNC_RUNS_UNAVAILABLE",
            "async run queue is temporarily unavailable",
        )
