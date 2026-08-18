from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql

from app.services.run_history import RunHistoryStore

from tests.test_run_history_storage import make_result


def test_mysql_url_selects_mysql_engine_without_connecting() -> None:
    store = RunHistoryStore(
        database_url="mysql+pymysql://synthetic-user:synthetic-password@db/app"
    )

    assert store.database_url.startswith("mysql+pymysql://")
    assert store.database_path is None
    assert store.engine.dialect.name == "mysql"
    assert store.test_runs.dialect_options["mysql"]["engine"] == "InnoDB"


def test_async_queue_tables_compile_as_innodb_without_request_payload_columns() -> None:
    run_queue_store = importlib.import_module("app.services.run_queue_store")
    for table in (run_queue_store.RUN_JOBS, run_queue_store.RUN_OUTBOX):
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        assert "ENGINE=InnoDB" in ddl
        assert "base_url" not in ddl
        assert "request_json" not in ddl
        assert "headers" not in ddl
        assert "variables" not in ddl

    assert run_queue_store.RUN_JOBS.c.run_id.primary_key is True
    assert run_queue_store.RUN_OUTBOX.c.outbox_id.primary_key is True


def test_sqlite_database_url_round_trips_with_the_same_store_interface(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "url-history.sqlite3"
    store = RunHistoryStore(database_url=f"sqlite:///{database_path}")
    result = make_result()

    store.save(result)

    assert store.get(result.run_id) is not None


def test_concurrent_sqlite_writes_keep_all_results(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent-history.sqlite3"

    def save_one(index: int) -> str:
        store = RunHistoryStore(database_path)
        result = make_result(name=f"case-{index}")
        store.save(result)
        return str(result.run_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        run_ids = list(executor.map(save_one, range(20)))

    store = RunHistoryStore(database_path)
    items, total = store.list(limit=100)

    assert total == 20
    assert len(items) == 20
    assert {str(item.run_id) for item in items} == set(run_ids)
