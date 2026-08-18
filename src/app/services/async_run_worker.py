from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from math import ceil
from threading import BoundedSemaphore, Event, Lock, Thread
from uuid import UUID, uuid4

from app.config import Settings, target_is_allowed
from app.schemas import TestRunRequest
from app.services.executor import TestExecutor
from app.services.redis_run_queue import (
    AsyncRunQueueMessageError,
    AsyncRunQueueUnavailable,
    RedisCapacityLease,
    RedisRunQueue,
    RedisStreamMessage,
)
from app.services.run_history import HistoryStorageError, RunHistoryStore
from app.services.run_queue_store import (
    JobToken,
    JobRecord,
    RunQueueStorageError,
    RunQueueStore,
)


_INFRASTRUCTURE_RESULT_CODES = frozenset(
    {"REQUEST_TIMEOUT", "CONNECT_FAILED", "NETWORK_ERROR"}
)


class AsyncRunWorker:
    def __init__(
        self,
        settings: Settings,
        queue: RedisRunQueue,
        queue_store: RunQueueStore,
        history_store: RunHistoryStore,
        *,
        executor_factory=TestExecutor,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.queue_store = queue_store
        self.history_store = history_store
        self.executor_factory = executor_factory
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self._lease_seconds = max(
            settings.async_job_lease_seconds,
            ceil(
                settings.run_budget_seconds
                + settings.request_timeout_seconds
                + 2
            ),
        )
        self._local_slots = BoundedSemaphore(
            settings.async_worker_concurrency
        )
        self._dispatch_lock = Lock()

    def run_once(self) -> bool:
        if not self._local_slots.acquire(blocking=False):
            return False
        try:
            try:
                self.queue.ensure_group()
                self._dispatch_outbox()
                message = self._next_message()
            except AsyncRunQueueUnavailable:
                return False
            if message is None:
                return False
            return self._handle_message(message)
        finally:
            self._local_slots.release()

    def run_forever(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self.queue.ping()
        self.queue.ensure_group()
        futures = set()
        with ThreadPoolExecutor(
            max_workers=self.settings.async_worker_concurrency,
            thread_name_prefix="async-run-worker",
        ) as executor:
            while not stop.is_set():
                while (
                    len(futures) < self.settings.async_worker_concurrency
                    and not stop.is_set()
                ):
                    futures.add(executor.submit(self.run_once))
                if not futures:
                    stop.wait(0.1)
                    continue
                done, futures = wait(
                    futures,
                    timeout=0.2,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    try:
                        future.result()
                    except Exception:
                        continue
            for future in futures:
                future.cancel()

    def _dispatch_outbox(self) -> None:
        with self._dispatch_lock:
            try:
                pending = self.queue_store.pending_outbox(limit=50)
            except RunQueueStorageError:
                return
            for record in pending:
                try:
                    self.queue.publish(
                        record.run_id,
                        record.attempt,
                        record.state_version,
                        outbox_id=record.outbox_id,
                    )
                except AsyncRunQueueUnavailable:
                    try:
                        self.queue_store.record_outbox_failure(
                            record.outbox_id,
                            "ASYNC_OUTBOX_PUBLISH_FAILED",
                            "async queue publication failed",
                            _utcnow() + timedelta(seconds=2),
                        )
                    except RunQueueStorageError:
                        pass
                    continue
                try:
                    self.queue_store.mark_outbox_published(
                        record.outbox_id
                    )
                except RunQueueStorageError:
                    continue

    def _next_message(self) -> RedisStreamMessage | None:
        try:
            reclaimed = self.queue.claim_expired(
                self.worker_id,
                self._lease_seconds * 1000,
                count=1,
            )
            if reclaimed:
                return reclaimed[0]
            messages = self.queue.read(
                self.worker_id,
                count=1,
                block_ms=100,
            )
            return messages[0] if messages else None
        except AsyncRunQueueUnavailable:
            return None
        except AsyncRunQueueMessageError as exc:
            if exc.message_id is not None:
                try:
                    self.queue.ack(exc.message_id)
                except AsyncRunQueueUnavailable:
                    pass
            return None

    def _handle_message(self, message: RedisStreamMessage) -> bool:
        try:
            lease = self.queue.acquire_capacity(
                self.worker_id,
                message.run_id,
                self.settings.async_max_active_runs,
                self._lease_seconds,
            )
        except AsyncRunQueueUnavailable:
            return False
        if lease is None:
            return False

        token: JobToken | None = None
        try:
            try:
                token = self.queue_store.claim_job(
                    message.run_id,
                    self.worker_id,
                    self._lease_seconds,
                    state_version=message.state_version,
                    attempt=message.attempt,
                )
            except RunQueueStorageError:
                return False
            if token is None:
                return self._handle_duplicate_message(message)
            if token.recovered:
                try:
                    if not self.queue.ack(message.message_id):
                        return False
                except AsyncRunQueueUnavailable:
                    return False
            return self._execute_claim(message, token, lease)
        finally:
            try:
                lease.release()
            except AsyncRunQueueUnavailable:
                pass

    def _handle_duplicate_message(self, message: RedisStreamMessage) -> bool:
        try:
            record = self.queue_store.get_status(message.run_id)
        except RunQueueStorageError:
            return False
        if record is None:
            try:
                return self.queue.ack(message.message_id)
            except AsyncRunQueueUnavailable:
                return False
        if record.state.value in {"completed", "failed"}:
            try:
                self.queue.ack(message.message_id)
            except AsyncRunQueueUnavailable:
                return False
            return True
        if (
            record.state_version > message.state_version
            or record.attempt > message.attempt
        ):
            try:
                return self.queue.ack(message.message_id)
            except AsyncRunQueueUnavailable:
                return False
        return False

    def _execute_claim(
        self,
        message: RedisStreamMessage,
        token: JobToken,
        lease: RedisCapacityLease,
    ) -> bool:
        try:
            record = self.queue_store.get_status(token.run_id)
        except RunQueueStorageError:
            return self._retry_or_fail(
                message,
                token,
                lease,
                "ASYNC_STORAGE_FAILED",
            )
        if record is None:
            return False

        try:
            payload_json = self.queue.get_payload(record.payload_key)
        except AsyncRunQueueUnavailable:
            return self._retry_or_fail(
                message,
                token,
                lease,
                "ASYNC_PAYLOAD_UNAVAILABLE",
            )
        if payload_json is None:
            return self._terminal_failure(
                message,
                token,
                "ASYNC_PAYLOAD_MISSING",
                "async run payload is missing",
            )

        try:
            request = TestRunRequest.model_validate_json(payload_json)
        except (JSONDecodeError, TypeError, ValueError):
            return self._terminal_failure(
                message,
                token,
                "ASYNC_PAYLOAD_INVALID",
                "async run payload is invalid",
            )
        if not target_is_allowed(str(request.base_url), self.settings):
            return self._terminal_failure(
                message,
                token,
                "ASYNC_TARGET_NOT_ALLOWED",
                "async run target is not allowed",
            )

        try:
            existing = self.history_store.get(token.run_id)
        except HistoryStorageError:
            return self._retry_or_fail(
                message,
                token,
                lease,
                "ASYNC_HISTORY_UNAVAILABLE",
            )
        if existing is not None:
            return self._complete_terminal(message, token, record)

        heartbeat_stop, lease_lost, heartbeat_thread = self._start_heartbeat(
            token,
            lease,
        )
        try:
            if lease_lost.is_set():
                return False
            try:
                executor = self.executor_factory(
                    timeout_seconds=self.settings.request_timeout_seconds,
                    run_budget_seconds=self.settings.run_budget_seconds,
                )
                result = executor.run(
                    str(request.base_url),
                    request.cases,
                    variables=request.variables,
                    secret_variables=request.secret_variables,
                )
                result = result.model_copy(update={"run_id": token.run_id})
                if lease_lost.is_set():
                    return False
                if _has_infrastructure_failure(result):
                    return self._retry_or_fail(
                        message,
                        token,
                        lease,
                        "ASYNC_EXECUTION_FAILED",
                    )
                try:
                    persisted = self.history_store.save_if_owned(
                        result,
                        owner_id=self.worker_id,
                        state_version=token.state_version,
                    )
                except HistoryStorageError:
                    return self._retry_or_fail(
                        message,
                        token,
                        lease,
                        "ASYNC_HISTORY_UNAVAILABLE",
                    )
                if not persisted:
                    return False
                if lease_lost.is_set():
                    return False
            except Exception:
                if lease_lost.is_set():
                    return False
                return self._retry_or_fail(
                    message,
                    token,
                    lease,
                    "ASYNC_EXECUTION_FAILED",
                )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
        return self._complete_terminal(message, token, record)

    def _complete_terminal(
        self,
        message: RedisStreamMessage,
        token: JobToken,
        record: JobRecord,
    ) -> bool:
        try:
            completed = self.queue_store.complete_job(
                token.run_id,
                self.worker_id,
                state_version=token.state_version,
            )
            if not completed:
                return False
            self.queue.delete_payload(record.payload_key)
            return self.queue.ack(message.message_id)
        except (RunQueueStorageError, AsyncRunQueueUnavailable):
            return False

    def _terminal_failure(
        self,
        message: RedisStreamMessage,
        token: JobToken,
        error_code: str,
        error_message: str,
    ) -> bool:
        try:
            failed = self.queue_store.fail_job(
                token.run_id,
                self.worker_id,
                error_code,
                error_message,
                state_version=token.state_version,
            )
            if not failed:
                return False
            record = self.queue_store.get_status(token.run_id)
            if record is not None:
                self.queue.delete_payload(record.payload_key)
            return self.queue.ack(message.message_id)
        except (RunQueueStorageError, AsyncRunQueueUnavailable):
            return False

    def _retry_or_fail(
        self,
        message: RedisStreamMessage,
        token: JobToken,
        lease: RedisCapacityLease,
        error_code: str,
    ) -> bool:
        if token.attempt < self.settings.async_max_attempts:
            delay = min(30, 2 ** max(0, token.attempt - 1))
            try:
                scheduled = self.queue_store.schedule_retry(
                    token.run_id,
                    self.worker_id,
                    error_code,
                    "async execution failed",
                    _utcnow() + timedelta(seconds=delay),
                    state_version=token.state_version,
                )
                if not scheduled:
                    return False
                return self.queue.ack(message.message_id)
            except (RunQueueStorageError, AsyncRunQueueUnavailable):
                return False
        return self._terminal_failure(
            message,
            token,
            "ASYNC_EXECUTION_FAILED",
            "async execution failed",
        )

    def _start_heartbeat(
        self,
        token: JobToken,
        lease: RedisCapacityLease,
    ) -> tuple[Event, Event, Thread]:
        stop = Event()
        lease_lost = Event()
        interval = max(1.0, self._lease_seconds / 3)

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    if not self.queue_store.renew_job(
                        token.run_id,
                        self.worker_id,
                        self._lease_seconds,
                        state_version=token.state_version,
                    ):
                        lease_lost.set()
                        return
                    if not lease.renew():
                        lease_lost.set()
                        return
                except (RunQueueStorageError, AsyncRunQueueUnavailable):
                    lease_lost.set()
                    return

        thread = Thread(
            target=heartbeat,
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        thread.start()
        return stop, lease_lost, thread


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _has_infrastructure_failure(result) -> bool:
    return any(
        (case.error_code or "").upper() in _INFRASTRUCTURE_RESULT_CODES
        for case in result.cases
    )
