from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path
from threading import Lock
from uuid import UUID

from app.schemas import TestRunResult, TestRunSummary


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATABASE_PATH = REPOSITORY_ROOT / ".data" / "run-history.sqlite3"
SCHEMA_VERSION = 1
MAX_RECORDS = 500
BUSY_TIMEOUT_MS = 5000


class HistoryStorageError(RuntimeError):
    """Raised when local run history cannot be read or written."""


class RunHistoryStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self._uses_production_path = database_path is None
        self.database_path = (database_path or DATABASE_PATH).resolve()
        self._initialization_lock = Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return

        with self._initialization_lock:
            if self._initialized:
                return
            try:
                if (
                    self._uses_production_path
                    and not self.database_path.is_relative_to(
                        REPOSITORY_ROOT.resolve()
                    )
                ):
                    raise OSError("history path resolves outside repository")
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
                with closing(self._raw_connection()) as connection:
                    with connection:
                        journal_mode = connection.execute(
                            "PRAGMA journal_mode=WAL"
                        ).fetchone()
                        if (
                            not journal_mode
                            or journal_mode[0].lower() != "wal"
                        ):
                            raise sqlite3.OperationalError(
                                "WAL journal mode is unavailable"
                            )
                        version = connection.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0]
                        if version not in {0, SCHEMA_VERSION}:
                            raise sqlite3.DatabaseError(
                                "unsupported history schema version"
                            )
                        connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS test_runs (
                                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                                run_id TEXT NOT NULL UNIQUE,
                                created_at TEXT NOT NULL,
                                passed INTEGER NOT NULL
                                    CHECK (passed IN (0, 1)),
                                total INTEGER NOT NULL CHECK (total >= 0),
                                passed_count INTEGER NOT NULL
                                    CHECK (passed_count >= 0),
                                failed_count INTEGER NOT NULL
                                    CHECK (failed_count >= 0),
                                skipped_count INTEGER NOT NULL
                                    CHECK (skipped_count >= 0),
                                duration_ms REAL NOT NULL
                                    CHECK (duration_ms >= 0),
                                result_json TEXT NOT NULL,
                                schema_version INTEGER NOT NULL,
                                CHECK (
                                    total = passed_count
                                        + failed_count
                                        + skipped_count
                                )
                            )
                            """
                        )
                        connection.execute(
                            """
                            CREATE INDEX IF NOT EXISTS
                                idx_test_runs_created_sequence
                            ON test_runs(created_at DESC, sequence DESC)
                            """
                        )
                        if version == 0:
                            connection.execute(
                                f"PRAGMA user_version={SCHEMA_VERSION}"
                            )
                self._initialized = True
            except (OSError, sqlite3.Error) as exc:
                raise HistoryStorageError(
                    "run history storage is unavailable"
                ) from exc

    def save(self, result: TestRunResult) -> None:
        self.initialize()
        created_at = (
            result.created_at.isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        try:
            serialized = result.model_dump_json()
            with closing(self._connection()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO test_runs (
                            run_id,
                            created_at,
                            passed,
                            total,
                            passed_count,
                            failed_count,
                            skipped_count,
                            duration_ms,
                            result_json,
                            schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(result.run_id),
                            created_at,
                            int(result.passed),
                            result.total,
                            result.passed_count,
                            result.failed_count,
                            result.skipped_count,
                            result.duration_ms,
                            serialized,
                            SCHEMA_VERSION,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM test_runs
                        WHERE sequence NOT IN (
                            SELECT sequence
                            FROM test_runs
                            ORDER BY created_at DESC, sequence DESC
                            LIMIT ?
                        )
                        """,
                        (MAX_RECORDS,),
                    )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryStorageError(
                "run history result could not be saved"
            ) from exc

    def list(self, limit: int) -> tuple[list[TestRunSummary], int]:
        self.initialize()
        try:
            with closing(self._connection()) as connection:
                with connection:
                    connection.execute("BEGIN")
                    total = connection.execute(
                        "SELECT COUNT(*) FROM test_runs"
                    ).fetchone()[0]
                    rows = connection.execute(
                        """
                        SELECT
                            run_id,
                            created_at,
                            passed,
                            total,
                            passed_count,
                            failed_count,
                            skipped_count,
                            duration_ms
                        FROM test_runs
                        ORDER BY created_at DESC, sequence DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
            items = [
                TestRunSummary.model_validate(dict(row))
                for row in rows
            ]
            return items, total
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryStorageError(
                "run history could not be read"
            ) from exc

    def get(self, run_id: UUID) -> TestRunResult | None:
        self.initialize()
        try:
            with closing(self._connection()) as connection:
                row = connection.execute(
                    """
                    SELECT result_json
                    FROM test_runs
                    WHERE run_id = ?
                    """,
                    (str(run_id),),
                ).fetchone()
            if row is None:
                return None
            return TestRunResult.model_validate_json(row["result_json"])
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryStorageError(
                "run history could not be read"
            ) from exc

    def _connection(self) -> sqlite3.Connection:
        return self._raw_connection()

    def _raw_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
