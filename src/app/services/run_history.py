from __future__ import annotations

from pathlib import Path
from threading import Lock
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.schemas import TestRunResult, TestRunSummary


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATABASE_PATH = REPOSITORY_ROOT / ".data" / "run-history.sqlite3"
SCHEMA_VERSION = 1
MAX_RECORDS = 500
BUSY_TIMEOUT_MS = 5000
_DATABASE_INITIALIZATION_LOCK = Lock()


HISTORY_METADATA = MetaData()
TEST_RUNS = Table(
    "test_runs",
    HISTORY_METADATA,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False, unique=True),
    Column("created_at", String(40), nullable=False),
    Column("passed", Integer, nullable=False),
    Column("total", Integer, nullable=False),
    Column("passed_count", Integer, nullable=False),
    Column("failed_count", Integer, nullable=False),
    Column("skipped_count", Integer, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column(
        "result_json",
        Text().with_variant(mysql.LONGTEXT(), "mysql"),
        nullable=False,
    ),
    Column("schema_version", Integer, nullable=False),
    CheckConstraint("passed IN (0, 1)", name="ck_test_runs_passed"),
    CheckConstraint("total >= 0", name="ck_test_runs_total"),
    CheckConstraint(
        "passed_count >= 0",
        name="ck_test_runs_passed_count",
    ),
    CheckConstraint(
        "failed_count >= 0",
        name="ck_test_runs_failed_count",
    ),
    CheckConstraint(
        "skipped_count >= 0",
        name="ck_test_runs_skipped_count",
    ),
    CheckConstraint("duration_ms >= 0", name="ck_test_runs_duration"),
    CheckConstraint(
        "total = passed_count + failed_count + skipped_count",
        name="ck_test_runs_counts",
    ),
    mysql_engine="InnoDB",
)
Index(
    "idx_test_runs_created_sequence",
    TEST_RUNS.c.created_at.desc(),
    TEST_RUNS.c.sequence.desc(),
)


class HistoryStorageError(RuntimeError):
    """Raised when run history cannot be read or written."""


class RunHistoryStore:
    def __init__(
        self,
        database_path: Path | None = None,
        *,
        database_url: str | None = None,
    ) -> None:
        if database_path is not None and database_url is not None:
            raise ValueError("database_path and database_url are mutually exclusive")

        if database_url is None:
            path = (database_path or DATABASE_PATH).resolve()
            self.database_url = _sqlite_url(path)
            self.database_path: Path | None = path
            self._uses_default_path = database_path is None
        else:
            self.database_url = database_url.strip()
            self.database_path = _sqlite_path_from_url(self.database_url)
            self._uses_default_path = False

        self.engine = self._create_engine()
        self.test_runs = TEST_RUNS
        self._initialization_lock = Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        with self._initialization_lock:
            if self._initialized:
                return
            with _DATABASE_INITIALIZATION_LOCK:
                if self._initialized:
                    return
                try:
                    if (
                        self._uses_default_path
                        and self.database_path is not None
                        and not self.database_path.is_relative_to(
                            REPOSITORY_ROOT.resolve()
                        )
                    ):
                        raise OSError("history path resolves outside repository")
                    if self.database_path is not None:
                        self.database_path.parent.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                    schema_version = self._sqlite_schema_version()
                    if schema_version not in {0, SCHEMA_VERSION}:
                        raise ValueError("unsupported history schema version")

                    with self.engine.begin() as connection:
                        HISTORY_METADATA.create_all(connection)
                        if self._is_sqlite and schema_version == 0:
                            connection.exec_driver_sql(
                                f"PRAGMA user_version={SCHEMA_VERSION}"
                            )
                    self._initialized = True
                except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
                    raise HistoryStorageError(
                        "run history storage is unavailable"
                    ) from exc

    def save(self, result: TestRunResult) -> None:
        self.initialize()
        created_at = (
            result.created_at.isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        values = {
            "run_id": str(result.run_id),
            "created_at": created_at,
            "passed": int(result.passed),
            "total": result.total,
            "passed_count": result.passed_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
            "result_json": result.model_dump_json(),
            "schema_version": SCHEMA_VERSION,
        }
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(TEST_RUNS).values(values))
                kept_sequences = connection.execute(
                    select(TEST_RUNS.c.sequence)
                    .order_by(
                        TEST_RUNS.c.created_at.desc(),
                        TEST_RUNS.c.sequence.desc(),
                    )
                    .limit(MAX_RECORDS)
                ).scalars().all()
                if kept_sequences:
                    connection.execute(
                        delete(TEST_RUNS).where(
                            TEST_RUNS.c.sequence.not_in(kept_sequences)
                        )
                    )
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise HistoryStorageError(
                "run history result could not be saved"
            ) from exc

    def save_if_owned(
        self,
        result: TestRunResult,
        *,
        owner_id: str,
        state_version: int,
    ) -> bool:
        """Persist a result only while its durable execution lease is valid."""
        self.initialize()
        normalized_owner = owner_id.strip()
        if not normalized_owner or len(normalized_owner) > 128:
            raise ValueError("owner_id must be a non-empty bounded string")
        if state_version < 0:
            raise ValueError("state_version must not be negative")

        created_at = (
            result.created_at.isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        values = {
            "run_id": str(result.run_id),
            "created_at": created_at,
            "passed": int(result.passed),
            "total": result.total,
            "passed_count": result.passed_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
            "duration_ms": result.duration_ms,
            "result_json": result.model_dump_json(),
            "schema_version": SCHEMA_VERSION,
        }
        now_value = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        try:
            from app.services.run_queue_store import RUN_JOBS

            with self.engine.begin() as connection:
                job = connection.execute(
                    select(
                        RUN_JOBS.c.state,
                        RUN_JOBS.c.lease_owner,
                        RUN_JOBS.c.state_version,
                        RUN_JOBS.c.lease_expires_at,
                    )
                    .where(RUN_JOBS.c.run_id == str(result.run_id))
                    .with_for_update()
                ).mappings().one_or_none()
                if job is None:
                    return False
                if (
                    str(job["state"]) != "running"
                    or str(job["lease_owner"]) != normalized_owner
                    or int(job["state_version"]) != state_version
                    or job["lease_expires_at"] is None
                    or str(job["lease_expires_at"]) <= now_value
                ):
                    return False

                connection.execute(insert(TEST_RUNS).values(values))
                kept_sequences = connection.execute(
                    select(TEST_RUNS.c.sequence)
                    .order_by(
                        TEST_RUNS.c.created_at.desc(),
                        TEST_RUNS.c.sequence.desc(),
                    )
                    .limit(MAX_RECORDS)
                ).scalars().all()
                if kept_sequences:
                    connection.execute(
                        delete(TEST_RUNS).where(
                            TEST_RUNS.c.sequence.not_in(kept_sequences)
                        )
                    )
            return True
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise HistoryStorageError(
                "run history result could not be saved"
            ) from exc

    def list(self, limit: int) -> tuple[list[TestRunSummary], int]:
        self.initialize()
        try:
            with self.engine.begin() as connection:
                total = connection.execute(
                    select(func.count()).select_from(TEST_RUNS)
                ).scalar_one()
                rows = connection.execute(
                    select(
                        TEST_RUNS.c.run_id,
                        TEST_RUNS.c.created_at,
                        TEST_RUNS.c.passed,
                        TEST_RUNS.c.total,
                        TEST_RUNS.c.passed_count,
                        TEST_RUNS.c.failed_count,
                        TEST_RUNS.c.skipped_count,
                        TEST_RUNS.c.duration_ms,
                    )
                    .order_by(
                        TEST_RUNS.c.created_at.desc(),
                        TEST_RUNS.c.sequence.desc(),
                    )
                    .limit(limit)
                ).mappings().all()
            items = [
                TestRunSummary.model_validate(dict(row))
                for row in rows
            ]
            return items, int(total)
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise HistoryStorageError(
                "run history could not be read"
            ) from exc

    def get(self, run_id: UUID) -> TestRunResult | None:
        self.initialize()
        try:
            with self.engine.connect() as connection:
                serialized = connection.execute(
                    select(TEST_RUNS.c.result_json).where(
                        TEST_RUNS.c.run_id == str(run_id)
                    )
                ).scalar_one_or_none()
            if serialized is None:
                return None
            return TestRunResult.model_validate_json(serialized)
        except (OSError, SQLAlchemyError, TypeError, ValueError) as exc:
            raise HistoryStorageError(
                "run history could not be read"
            ) from exc

    @property
    def _is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    def _create_engine(self) -> Engine:
        url = make_url(self.database_url)
        if url.drivername not in {"sqlite", "mysql+pymysql"}:
            raise ValueError(
                "database_url must use sqlite or mysql+pymysql"
            )
        if url.drivername == "sqlite":
            if url.database != ":memory:":
                return create_engine(
                    self.database_url,
                    connect_args={
                        "check_same_thread": False,
                        "timeout": BUSY_TIMEOUT_MS / 1000,
                    },
                )
            return create_engine(self.database_url)
        return create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    def _sqlite_schema_version(self) -> int:
        if not self._is_sqlite:
            return SCHEMA_VERSION
        with self.engine.connect() as connection:
            connection.exec_driver_sql(
                "PRAGMA journal_mode=WAL"
            ).scalar_one()
            connection.commit()
            version = connection.exec_driver_sql(
                "PRAGMA user_version"
            ).scalar_one()
        return int(version)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _sqlite_path_from_url(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.drivername != "sqlite" or url.database in {None, ":memory:"}:
        return None
    return Path(url.database).resolve()
