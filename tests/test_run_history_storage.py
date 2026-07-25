from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.schemas import (
    AssertionResult,
    AssertionType,
    CaseResult,
    CaseStatus,
    TestRunResult as ApiTestRunResult,
)
from app.services.run_history import HistoryStorageError, RunHistoryStore


def make_result(
    *,
    created_at: datetime | None = None,
    passed: bool = True,
    name: str = "synthetic history case",
) -> ApiTestRunResult:
    case_passed = passed
    return ApiTestRunResult(
        created_at=created_at or datetime.now(timezone.utc),
        passed=passed,
        total=1,
        passed_count=1 if passed else 0,
        failed_count=0 if passed else 1,
        skipped_count=0,
        duration_ms=12.5,
        cases=[
            CaseResult(
                id="history_case",
                name=name,
                status=(
                    CaseStatus.PASSED if case_passed else CaseStatus.FAILED
                ),
                passed=case_passed,
                status_code=200,
                response_time_ms=4.5,
                assertions=[
                    AssertionResult(
                        type=AssertionType.STATUS_CODE,
                        passed=case_passed,
                        expected=200,
                        actual=200 if case_passed else 500,
                        message="synthetic assertion result",
                    )
                ],
            )
        ],
    )


def test_store_initializes_sqlite_schema_idempotently(tmp_path: Path) -> None:
    database_path = tmp_path / "isolated" / "history.sqlite3"
    store = RunHistoryStore(database_path)

    store.initialize()
    store.initialize()

    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert journal_mode.lower() == "wal"
    assert user_version == 1
    assert "test_runs" in tables


def test_store_round_trips_a_complete_safe_result(tmp_path: Path) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite3")
    result = make_result(
        created_at=datetime(2026, 7, 25, 11, 12, 13, 456789, timezone.utc),
        passed=False,
    )

    store.save(result)

    restored = store.get(result.run_id)
    assert restored is not None
    assert restored.model_dump(mode="json") == result.model_dump(mode="json")


def test_store_lists_newest_first_and_uses_sequence_as_tie_breaker(
    tmp_path: Path,
) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite3")
    shared_time = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    older = make_result(created_at=shared_time - timedelta(seconds=1))
    first_at_same_time = make_result(created_at=shared_time)
    second_at_same_time = make_result(created_at=shared_time)

    store.save(older)
    store.save(first_at_same_time)
    store.save(second_at_same_time)

    items, total = store.list(limit=10)

    assert total == 3
    assert [item.run_id for item in items] == [
        second_at_same_time.run_id,
        first_at_same_time.run_id,
        older.run_id,
    ]


def test_store_limit_truncates_items_but_total_counts_all_records(
    tmp_path: Path,
) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite3")
    results = [make_result() for _ in range(3)]
    for result in results:
        store.save(result)

    items, total = store.list(limit=1)

    assert len(items) == 1
    assert total == 3
    assert items[0].run_id == results[-1].run_id


def test_store_returns_none_for_an_unknown_run(tmp_path: Path) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite3")

    assert store.get(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")) is None


def test_store_instances_are_isolated_by_database_path(tmp_path: Path) -> None:
    first = RunHistoryStore(tmp_path / "first" / "history.sqlite3")
    second = RunHistoryStore(tmp_path / "second" / "history.sqlite3")
    result = make_result()

    first.save(result)

    assert first.get(result.run_id) is not None
    assert second.get(result.run_id) is None
    assert second.list(limit=20) == ([], 0)


def test_persisted_bytes_contain_only_the_already_redacted_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.sqlite3"
    store = RunHistoryStore(database_path)
    secret = "synthetic-secret-never-persist"
    forbidden_request_data = [
        secret,
        "https://forbidden-target.example.test",
        "x-synthetic-secret-header",
        '{"password":"synthetic-request-secret"}',
    ]
    result = make_result(name="[REDACTED]")

    store.save(result)

    database_bytes = b"".join(
        path.read_bytes()
        for path in database_path.parent.glob(f"{database_path.name}*")
    )
    for forbidden in forbidden_request_data:
        assert forbidden.encode() not in database_bytes
    assert b"[REDACTED]" in database_bytes


def test_corrupt_database_is_wrapped_without_exposing_sqlite_details(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.sqlite3"
    database_path.write_bytes(b"not a sqlite database")
    store = RunHistoryStore(database_path)

    with pytest.raises(HistoryStorageError) as captured:
        store.list(limit=20)

    message = str(captured.value)
    assert "sqlite" not in message.lower()
    assert str(database_path) not in message


def test_corrupt_result_json_is_wrapped_as_a_storage_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.sqlite3"
    store = RunHistoryStore(database_path)
    result = make_result()
    store.save(result)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE test_runs SET result_json = ? WHERE run_id = ?",
            ("{not-valid-json", str(result.run_id)),
        )

    with pytest.raises(HistoryStorageError):
        store.get(result.run_id)


def test_failed_insert_rolls_back_without_a_partial_record(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.sqlite3"
    store = RunHistoryStore(database_path)
    result = make_result()
    store.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_history_insert
            BEFORE INSERT ON test_runs
            BEGIN
                SELECT RAISE(ABORT, 'synthetic write failure');
            END
            """
        )

    with pytest.raises(HistoryStorageError):
        store.save(result)

    with sqlite3.connect(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM test_runs"
        ).fetchone()[0]
    assert count == 0
