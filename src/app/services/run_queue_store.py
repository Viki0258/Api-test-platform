from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from threading import Lock
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app.schemas import RunJobState
from app.services.run_history import DATABASE_PATH


QUEUE_METADATA = MetaData()
RUN_JOBS = Table(
    "run_jobs",
    QUEUE_METADATA,
    Column("run_id", String(36), primary_key=True),
    Column("payload_key", String(256), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt", Integer, nullable=False, default=0),
    Column("state_version", Integer, nullable=False, default=0),
    Column("created_at", String(40), nullable=False),
    Column("started_at", String(40), nullable=True),
    Column("finished_at", String(40), nullable=True),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at", String(40), nullable=True),
    Column("next_attempt_at", String(40), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("error_message", String(500), nullable=True),
    CheckConstraint(
        "state IN ('queued', 'running', 'completed', 'failed')",
        name="ck_run_jobs_state",
    ),
    CheckConstraint("attempt >= 0", name="ck_run_jobs_attempt"),
    CheckConstraint(
        "state_version >= 0",
        name="ck_run_jobs_state_version",
    ),
    mysql_engine="InnoDB",
)
Index(
    "idx_run_jobs_state_lease",
    RUN_JOBS.c.state,
    RUN_JOBS.c.lease_expires_at,
)

RUN_OUTBOX = Table(
    "run_outbox",
    QUEUE_METADATA,
    Column("outbox_id", String(36), primary_key=True),
    Column("run_id", String(36), nullable=False),
    Column("event_kind", String(32), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("state_version", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("next_attempt_at", String(40), nullable=False),
    Column("published_at", String(40), nullable=True),
    Column("publish_attempts", Integer, nullable=False, default=0),
    Column("error_code", String(64), nullable=True),
    Column("error_message", String(500), nullable=True),
    CheckConstraint("attempt >= 0", name="ck_run_outbox_attempt"),
    CheckConstraint(
        "state_version >= 0",
        name="ck_run_outbox_state_version",
    ),
    CheckConstraint(
        "publish_attempts >= 0",
        name="ck_run_outbox_publish_attempts",
    ),
    mysql_engine="InnoDB",
)
Index(
    "idx_run_outbox_pending",
    RUN_OUTBOX.c.published_at,
    RUN_OUTBOX.c.next_attempt_at,
)
Index(
    "idx_run_outbox_run_version",
    RUN_OUTBOX.c.run_id,
    RUN_OUTBOX.c.state_version,
)


QUEUE_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
QUEUE_BUSY_TIMEOUT_MS = 5000
_QUEUE_INITIALIZATION_LOCK = Lock()


class RunQueueStorageError(RuntimeError):
    """Raised when durable async queue state is unavailable."""


@dataclass(frozen=True)
class JobToken:
    run_id: UUID
    owner_id: str
    state_version: int
    attempt: int
    recovered: bool = False


@dataclass(frozen=True)
class JobRecord:
    run_id: UUID
    payload_key: str
    state: RunJobState
    attempt: int
    state_version: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: UUID
    run_id: UUID
    event_kind: str
    attempt: int
    state_version: int
    created_at: datetime
    next_attempt_at: datetime
    published_at: datetime | None
    publish_attempts: int
    error_code: str | None
    error_message: str | None


class RunQueueStore:
    def __init__(self, database_url: str | None = None) -> None:
        if database_url is None or not database_url.strip():
            database_path = DATABASE_PATH.resolve()
            self.database_url = _sqlite_url(database_path)
            self.database_path: Path | None = database_path
            self._uses_default_path = True
        else:
            self.database_url = database_url.strip()
            self.database_path = _sqlite_path_from_url(self.database_url)
            self._uses_default_path = False

        self.engine = self._create_engine()
        self._initialization_lock = Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        with self._initialization_lock:
            if self._initialized:
                return
            with _QUEUE_INITIALIZATION_LOCK:
                if self._initialized:
                    return
                try:
                    if (
                        self._uses_default_path
                        and self.database_path is not None
                        and not self.database_path.is_relative_to(
                            QUEUE_REPOSITORY_ROOT.resolve()
                        )
                    ):
                        raise OSError("queue path resolves outside repository")
                    if self.database_path is not None:
                        self.database_path.parent.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                    with self.engine.begin() as connection:
                        QUEUE_METADATA.create_all(connection)
                    self._initialized = True
                except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
                    raise RunQueueStorageError(
                        "async queue storage is unavailable"
                    ) from exc

    def create_job(self, run_id: UUID, payload_key: str) -> JobRecord:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        normalized_payload_key = payload_key.strip()
        if not normalized_payload_key or len(normalized_payload_key) > 256:
            raise ValueError("payload_key must be a non-empty bounded string")

        now = _utcnow()
        now_value = _encode_timestamp(now)
        outbox_id = uuid4()
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(RUN_JOBS).values(
                        run_id=str(normalized_run_id),
                        payload_key=normalized_payload_key,
                        state=RunJobState.QUEUED.value,
                        attempt=0,
                        state_version=0,
                        created_at=now_value,
                        next_attempt_at=now_value,
                    )
                )
                connection.execute(
                    insert(RUN_OUTBOX).values(
                        outbox_id=str(outbox_id),
                        run_id=str(normalized_run_id),
                        event_kind="submit",
                        attempt=0,
                        state_version=0,
                        created_at=now_value,
                        next_attempt_at=now_value,
                        publish_attempts=0,
                    )
                )
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue job could not be created"
            ) from exc

        record = self.get_status(normalized_run_id)
        if record is None:
            raise RunQueueStorageError("async queue job could not be read")
        return record

    def get_status(self, run_id: UUID) -> JobRecord | None:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(RUN_JOBS).where(
                        RUN_JOBS.c.run_id == str(normalized_run_id)
                    )
                ).mappings().one_or_none()
            return None if row is None else _job_record(row)
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue status could not be read"
            ) from exc

    def claim_job(
        self,
        run_id: UUID,
        owner_id: str,
        lease_seconds: int,
        *,
        state_version: int | None = None,
        attempt: int | None = None,
    ) -> JobToken | None:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        normalized_owner = _normalize_owner(owner_id)
        if lease_seconds < 0:
            raise ValueError("lease_seconds must not be negative")
        expected_version = _state_version(state_version)
        if attempt is not None and attempt < 0:
            raise ValueError("attempt must not be negative")
        now = _utcnow()
        now_value = _encode_timestamp(now)
        lease_value = _encode_timestamp(
            now + timedelta(seconds=lease_seconds)
        )
        try:
            with self.engine.begin() as connection:
                current = connection.execute(
                    select(
                        RUN_JOBS.c.state,
                        RUN_JOBS.c.state_version,
                        RUN_JOBS.c.attempt,
                        RUN_JOBS.c.lease_expires_at,
                        RUN_JOBS.c.next_attempt_at,
                    )
                    .where(
                        RUN_JOBS.c.run_id == str(normalized_run_id)
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if current is None:
                    return None

                current_state = str(current["state"])
                current_version = int(current["state_version"])
                current_attempt = int(current["attempt"])
                is_recovery = False

                if current_state == RunJobState.QUEUED.value:
                    if (
                        current["next_attempt_at"] is not None
                        and str(current["next_attempt_at"]) > now_value
                    ):
                        return None
                    if (
                        expected_version is not None
                        and current_version != expected_version
                    ):
                        return None
                    if attempt is not None and current_attempt != attempt:
                        return None
                elif current_state == RunJobState.RUNNING.value:
                    if (
                        current["lease_expires_at"] is None
                        or str(current["lease_expires_at"]) > now_value
                    ):
                        return None
                    if expected_version is None and attempt is None:
                        is_recovery = True
                    elif expected_version is None:
                        is_recovery = current_attempt == attempt
                    elif attempt is None:
                        is_recovery = current_version == expected_version + 1
                    else:
                        is_recovery = (
                            (
                                current_version == expected_version + 1
                                and current_attempt == attempt + 1
                            )
                            or (
                                current_version == expected_version
                                and current_attempt == attempt
                            )
                        )
                    if not is_recovery:
                        return None
                else:
                    return None

                predicates = [
                    RUN_JOBS.c.run_id == str(normalized_run_id),
                    RUN_JOBS.c.state == current_state,
                    RUN_JOBS.c.state_version == current_version,
                    RUN_JOBS.c.attempt == current_attempt,
                ]
                if current_state == RunJobState.QUEUED.value:
                    predicates.append(
                        or_(
                            RUN_JOBS.c.next_attempt_at.is_(None),
                            RUN_JOBS.c.next_attempt_at <= now_value,
                        )
                    )
                else:
                    predicates.append(
                        and_(
                            RUN_JOBS.c.lease_expires_at.is_not(None),
                            RUN_JOBS.c.lease_expires_at <= now_value,
                        )
                    )
                result = connection.execute(
                    update(RUN_JOBS)
                    .where(*predicates)
                    .values(
                        state=RunJobState.RUNNING.value,
                        attempt=RUN_JOBS.c.attempt + 1,
                        state_version=RUN_JOBS.c.state_version + 1,
                        started_at=now_value,
                        lease_owner=normalized_owner,
                        lease_expires_at=lease_value,
                        next_attempt_at=None,
                        error_code=None,
                        error_message=None,
                    )
                )
                if result.rowcount != 1:
                    return None
                next_attempt = current_attempt + 1
                next_version = current_version + 1
                if is_recovery:
                    connection.execute(
                        insert(RUN_OUTBOX).values(
                            outbox_id=str(uuid4()),
                            run_id=str(normalized_run_id),
                            event_kind="recovery",
                            attempt=next_attempt,
                            state_version=next_version,
                            created_at=now_value,
                            next_attempt_at=now_value,
                            publish_attempts=0,
                        )
                    )
                row = connection.execute(
                    select(
                        RUN_JOBS.c.run_id,
                        RUN_JOBS.c.state_version,
                        RUN_JOBS.c.attempt,
                    ).where(
                        RUN_JOBS.c.run_id == str(normalized_run_id)
                    )
                ).mappings().one()
                return JobToken(
                    run_id=normalized_run_id,
                    owner_id=normalized_owner,
                    state_version=int(row["state_version"]),
                    attempt=int(row["attempt"]),
                    recovered=is_recovery,
                )
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue job could not be claimed"
            ) from exc

    def renew_job(
        self,
        run_id: UUID,
        owner_id: str,
        lease_seconds: int,
        *,
        state_version: int | JobToken | None = None,
    ) -> bool:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        normalized_owner = _normalize_owner(owner_id)
        expected_version = _state_version(state_version)
        if lease_seconds < 0:
            raise ValueError("lease_seconds must not be negative")
        now = _utcnow()
        predicates = [
            RUN_JOBS.c.run_id == str(normalized_run_id),
            RUN_JOBS.c.state == RunJobState.RUNNING.value,
            RUN_JOBS.c.lease_owner == normalized_owner,
            RUN_JOBS.c.lease_expires_at.is_not(None),
            RUN_JOBS.c.lease_expires_at > _encode_timestamp(now),
        ]
        if expected_version is not None:
            predicates.append(
                RUN_JOBS.c.state_version == expected_version
            )
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(RUN_JOBS)
                    .where(*predicates)
                    .values(
                        lease_expires_at=_encode_timestamp(
                            now + timedelta(seconds=lease_seconds)
                        )
                    )
                )
            return result.rowcount == 1
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue lease could not be renewed"
            ) from exc

    def complete_job(
        self,
        run_id: UUID,
        owner_id: str,
        *,
        state_version: int | JobToken | None = None,
    ) -> bool:
        return self._terminal_update(
            run_id,
            owner_id,
            state_version=state_version,
            state=RunJobState.COMPLETED,
            error_code=None,
            error_message=None,
        )

    def schedule_retry(
        self,
        run_id: UUID,
        owner_id: str,
        error_code: str,
        error_message: str,
        next_attempt_at: datetime,
        *,
        state_version: int | JobToken | None = None,
    ) -> bool:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        normalized_owner = _normalize_owner(owner_id)
        expected_version = _state_version(state_version)
        next_attempt_value = _encode_timestamp(next_attempt_at)
        predicates = self._owned_running_predicates(
            normalized_run_id,
            normalized_owner,
            expected_version,
        )
        now_value = _encode_timestamp(_utcnow())
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(RUN_JOBS)
                    .where(*predicates)
                    .values(
                        state=RunJobState.QUEUED.value,
                        state_version=RUN_JOBS.c.state_version + 1,
                        lease_owner=None,
                        lease_expires_at=None,
                        next_attempt_at=next_attempt_value,
                        finished_at=None,
                        error_code=None,
                        error_message=None,
                    )
                )
                if result.rowcount != 1:
                    return False
                row = connection.execute(
                    select(
                        RUN_JOBS.c.attempt,
                        RUN_JOBS.c.state_version,
                    ).where(
                        RUN_JOBS.c.run_id == str(normalized_run_id)
                    )
                ).mappings().one()
                connection.execute(
                    insert(RUN_OUTBOX).values(
                        outbox_id=str(uuid4()),
                        run_id=str(normalized_run_id),
                        event_kind="retry",
                        attempt=int(row["attempt"]),
                        state_version=int(row["state_version"]),
                        created_at=now_value,
                        next_attempt_at=next_attempt_value,
                        publish_attempts=0,
                        error_code=_sanitize_error_code(error_code),
                        error_message=_sanitize_error_message(error_message),
                    )
                )
            return True
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue retry could not be scheduled"
            ) from exc

    def fail_job(
        self,
        run_id: UUID,
        owner_id: str,
        error_code: str,
        error_message: str,
        *,
        state_version: int | JobToken | None = None,
    ) -> bool:
        return self._terminal_update(
            run_id,
            owner_id,
            state_version=state_version,
            state=RunJobState.FAILED,
            error_code=_sanitize_error_code(error_code),
            error_message=_sanitize_error_message(error_message),
        )

    def pending_outbox(self, limit: int) -> list[OutboxRecord]:
        self.initialize()
        if limit < 1:
            return []
        now_value = _encode_timestamp(_utcnow())
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    select(RUN_OUTBOX)
                    .where(
                        RUN_OUTBOX.c.published_at.is_(None),
                        RUN_OUTBOX.c.next_attempt_at <= now_value,
                    )
                    .order_by(
                        RUN_OUTBOX.c.created_at.asc(),
                        RUN_OUTBOX.c.outbox_id.asc(),
                    )
                    .limit(min(limit, 1000))
                ).mappings().all()
            return [_outbox_record(row) for row in rows]
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue outbox could not be read"
            ) from exc

    def mark_outbox_published(self, outbox_id: UUID) -> bool:
        self.initialize()
        normalized_outbox_id = _normalize_run_id(outbox_id)
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(RUN_OUTBOX)
                    .where(
                        RUN_OUTBOX.c.outbox_id == str(normalized_outbox_id),
                        RUN_OUTBOX.c.published_at.is_(None),
                    )
                    .values(
                        published_at=_encode_timestamp(_utcnow()),
                        error_code=None,
                        error_message=None,
                    )
                )
            return result.rowcount == 1
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue outbox could not be marked"
            ) from exc

    def record_outbox_failure(
        self,
        outbox_id: UUID,
        error_code: str,
        error_message: str,
        next_attempt_at: datetime,
    ) -> bool:
        self.initialize()
        normalized_outbox_id = _normalize_run_id(outbox_id)
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(RUN_OUTBOX)
                    .where(
                        RUN_OUTBOX.c.outbox_id == str(normalized_outbox_id),
                        RUN_OUTBOX.c.published_at.is_(None),
                    )
                    .values(
                        publish_attempts=RUN_OUTBOX.c.publish_attempts + 1,
                        next_attempt_at=_encode_timestamp(next_attempt_at),
                        error_code=_sanitize_error_code(error_code),
                        error_message=_sanitize_error_message(error_message),
                    )
                )
            return result.rowcount == 1
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue outbox failure could not be recorded"
            ) from exc

    def _terminal_update(
        self,
        run_id: UUID,
        owner_id: str,
        *,
        state_version: int | JobToken | None,
        state: RunJobState,
        error_code: str | None,
        error_message: str | None,
    ) -> bool:
        self.initialize()
        normalized_run_id = _normalize_run_id(run_id)
        normalized_owner = _normalize_owner(owner_id)
        expected_version = _state_version(state_version)
        predicates = self._owned_running_predicates(
            normalized_run_id,
            normalized_owner,
            expected_version,
        )
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(RUN_JOBS)
                    .where(*predicates)
                    .values(
                        state=state.value,
                        state_version=RUN_JOBS.c.state_version + 1,
                        finished_at=_encode_timestamp(_utcnow()),
                        lease_owner=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        error_code=error_code,
                        error_message=error_message,
                    )
                )
            return result.rowcount == 1
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise RunQueueStorageError(
                "async queue job transition could not be saved"
            ) from exc

    @staticmethod
    def _owned_running_predicates(
        run_id: UUID,
        owner_id: str,
        state_version: int | JobToken | None,
    ) -> list[object]:
        expected_version = _state_version(state_version)
        predicates: list[object] = [
            RUN_JOBS.c.run_id == str(run_id),
            RUN_JOBS.c.state == RunJobState.RUNNING.value,
            RUN_JOBS.c.lease_owner == owner_id,
            RUN_JOBS.c.lease_expires_at.is_not(None),
            RUN_JOBS.c.lease_expires_at > _encode_timestamp(_utcnow()),
        ]
        if expected_version is not None:
            predicates.append(
                RUN_JOBS.c.state_version == expected_version
            )
        return predicates

    def _create_engine(self) -> Engine:
        url = make_url(self.database_url)
        if url.drivername not in {"sqlite", "mysql+pymysql"}:
            raise ValueError(
                "database_url must use sqlite or mysql+pymysql"
            )
        if url.drivername == "sqlite":
            if url.database == ":memory:":
                return create_engine(
                    self.database_url,
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool,
                )
            return create_engine(
                self.database_url,
                connect_args={
                    "check_same_thread": False,
                    "timeout": QUEUE_BUSY_TIMEOUT_MS / 1000,
                },
            )
        return create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
        )


def _normalize_run_id(run_id: UUID) -> UUID:
    normalized = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    if normalized.version != 4:
        raise ValueError("run_id must be UUIDv4")
    return normalized


def _normalize_owner(owner_id: str) -> str:
    normalized = owner_id.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("owner_id must be a non-empty bounded string")
    return normalized


def _state_version(value: int | JobToken | None) -> int | None:
    if isinstance(value, JobToken):
        return value.state_version
    if value is None:
        return None
    if value < 0:
        raise ValueError("state_version must not be negative")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _encode_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _decode_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _job_record(row) -> JobRecord:
    return JobRecord(
        run_id=UUID(str(row["run_id"])),
        payload_key=str(row["payload_key"]),
        state=RunJobState(str(row["state"])),
        attempt=int(row["attempt"]),
        state_version=int(row["state_version"]),
        created_at=_decode_timestamp(row["created_at"]),
        started_at=_decode_timestamp(row["started_at"]),
        finished_at=_decode_timestamp(row["finished_at"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=_decode_timestamp(row["lease_expires_at"]),
        next_attempt_at=_decode_timestamp(row["next_attempt_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _outbox_record(row) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=UUID(str(row["outbox_id"])),
        run_id=UUID(str(row["run_id"])),
        event_kind=str(row["event_kind"]),
        attempt=int(row["attempt"]),
        state_version=int(row["state_version"]),
        created_at=_decode_timestamp(row["created_at"]),
        next_attempt_at=_decode_timestamp(row["next_attempt_at"]),
        published_at=_decode_timestamp(row["published_at"]),
        publish_attempts=int(row["publish_attempts"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _sanitize_error_code(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_")
    normalized = normalized.upper()[:64]
    return normalized or "ASYNC_RUN_FAILED"


def _sanitize_error_message(value: str) -> str:
    return " ".join(str(value).split())[:500]


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _sqlite_path_from_url(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.drivername != "sqlite" or url.database in {None, ":memory:"}:
        return None
    return Path(url.database).resolve()
