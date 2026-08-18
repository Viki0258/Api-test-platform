# MySQL Concurrent History Implementation Plan

> For agentic workers: use a task-by-task implementation workflow. Steps use checkbox syntax for tracking.

Goal: Add selectable MySQL history storage and bounded multi-client run concurrency without changing one-run execution semantics.

Architecture: Keep RunHistoryStore as the application-facing storage boundary and implement it with SQLAlchemy Core metadata so SQLite remains the default local adapter and mysql+pymysql is selected by DATABASE_URL. Add a process-local RunCapacityLimiter dependency around the existing synchronous run endpoint; acquire after validation, reject immediately at capacity, and release in finally.

Tech Stack: Python 3.11, FastAPI, Pydantic Settings, SQLAlchemy Core 2.x, PyMySQL, SQLite, pytest, HTTPX.

Spec: docs/comet/changes/audit-current-project/specs/concurrent-history/spec.md

## Global Constraints

- DATABASE_URL is optional; unset behavior remains repository-local .data/run-history.sqlite3.
- MySQL URLs use mysql+pymysql; credentials are supplied only through an uncommitted .env.
- MAX_CONCURRENT_RUNS defaults to 4 and is bounded to 1..64; capacity rejection is immediate HTTP 429 with RUN_CAPACITY_EXCEEDED and positive Retry-After.
- Run execution stays sequential within a run; independent clients may overlap until capacity is full.
- No authentication, tenant isolation, distributed lease, queue, background 202, production connection, migration, deployment, push, or destructive cleanup.
- Every production behavior change follows a failing-test-first cycle; tests use synthetic data and SQLite or SQLAlchemy dialect compilation only.

---

### Task 1: Freeze configuration, storage, and concurrency contracts

Files:
- Modify: src/app/config.py
- Create: src/app/services/concurrency.py
- Modify: contracts/run-history-v0.2.yaml
- Test: tests/test_contract_validation.py and a new tests/test_concurrency.py

Interfaces:
- Settings.database_url: str | None accepts only empty/SQLite/MySQL-PyMySQL URLs.
- Settings.max_concurrent_runs: int defaults to 4 and is constrained to 1..64.
- RunCapacityLimiter(limit: int), try_acquire() -> bool, and release() -> None provide a thread-safe nonblocking capacity boundary.

- [ ] Step 1: Write the failing configuration and limiter tests.

~~~python
def test_settings_expose_mysql_and_bounded_run_capacity():
    settings = Settings(database_url="mysql+pymysql://db/app", max_concurrent_runs=4)
    assert settings.database_url.startswith("mysql+pymysql://")
    assert settings.max_concurrent_runs == 4

def test_capacity_limiter_rejects_until_a_slot_is_released():
    limiter = RunCapacityLimiter(1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    limiter.release()
    assert limiter.try_acquire() is True
~~~

- [ ] Step 2: Run .\.venv\Scripts\python.exe -m pytest tests/test_contract_validation.py tests/test_concurrency.py -q and confirm failure because the new settings/limiter contract is absent.
- [ ] Step 3: Implement only the settings validation and semaphore-backed limiter.
- [ ] Step 4: Rerun the focused tests and confirm they pass.

### Task 2: Replace the storage internals with SQLite/MySQL SQLAlchemy Core

Files:
- Modify: pyproject.toml
- Modify: src/app/services/run_history.py
- Test: tests/test_run_history_storage.py
- Create: tests/test_mysql_storage.py

Interfaces:
- Preserve RunHistoryStore(database_path: Path | None = None) for existing SQLite tests.
- Add RunHistoryStore(database_url: str | None = None) for configured storage.
- Preserve initialize(), save(result), list(limit), and get(run_id) plus HistoryStorageError.

- [ ] Step 1: Add focused failing tests for MySQL URL dialect selection, SQLite fallback, concurrent SQLite saves, and sanitized storage errors.
- [ ] Step 2: Run .\.venv\Scripts\python.exe -m pytest tests/test_run_history_storage.py tests/test_mysql_storage.py -q and confirm the tests fail for the missing SQLAlchemy/MySQL adapter behavior.
- [ ] Step 3: Add SQLAlchemy and PyMySQL to the project dependency declaration and install only into .venv under the recorded approval.
- [ ] Step 4: Implement dialect-neutral table metadata, repository-local SQLite fallback, MySQL InnoDB-compatible schema, parameterized writes, deterministic ordering, retention, and sanitized exception wrapping.
- [ ] Step 5: Rerun the focused storage suite and confirm it passes without a real MySQL connection.

### Task 3: Wire bounded capacity into the run endpoint

Files:
- Modify: src/app/main.py
- Test: tests/test_run_history_api.py
- Test: tests/test_concurrency.py

Interfaces:
- get_run_capacity_limiter() returns the process-local configured limiter.
- POST /api/v1/runs acquires after target validation, returns 429 with Retry-After when full, and releases in finally around execution and persistence.

- [ ] Step 1: Write failing API tests proving capacity rejection does not call the executor/store and that a slot is released after executor or persistence failure.
- [ ] Step 2: Run the focused API tests and confirm the current endpoint has no capacity rejection or release behavior.
- [ ] Step 3: Inject the limiter into the endpoint and implement the stable 429 response plus finally release.
- [ ] Step 4: Rerun the API/concurrency tests and confirm they pass.

### Task 4: Wire configured storage and document operations

Files:
- Modify: src/app/main.py
- Modify: README.md
- Modify: .env.example
- Modify: tasks/active/audit-current-project.md
- Test: tests/test_run_history_api.py

Interfaces:
- The default history dependency constructs the store from Settings.database_url while dependency overrides continue to work in tests.
- README documents DATABASE_URL, MAX_CONCURRENT_RUNS, MySQL schema bootstrap/rollback boundaries, and safe local startup.

- [ ] Step 1: Write a failing dependency test showing the default store receives the configured database URL without exposing credentials.
- [ ] Step 2: Run the focused test and confirm the current dependency ignores DATABASE_URL.
- [ ] Step 3: Pass settings into the store dependency and add configuration documentation/examples.
- [ ] Step 4: Rerun the focused API and configuration tests.

### Task 5: Full verification and Native handoff

Files:
- Modify: docs/comet/changes/audit-current-project/brief.md
- Modify: docs/comet/changes/audit-current-project/comet-state.yaml only through Runtime
- Modify: tasks/active/audit-current-project.md

- [ ] Step 1: Run the full pytest/coverage, workspace validation, Python compilation, frontend syntax, pip check, and git diff --check commands.
- [ ] Step 2: Inspect the final diff and verify only scoped files changed; confirm no secret, production URL, or external file was touched.
- [ ] Step 3: Submit the Runtime Builder handoff with exact checks, known limits, and acceptance coverage, then run the Runtime-provided verifier continuation.
- [ ] Step 4: Run python .\scripts\complete_task.py audit-current-project --check only after all acceptance items and approvals are resolved.

## Plan self-review

- Coverage: A1-A8 map to Tasks 1-4 and final verification; no acceptance item depends on a real MySQL server.
- Consistency: the store constructor preserves the existing positional SQLite path while adding a keyword database URL; the limiter is acquired and released only in the endpoint.
- Scope: no worker queue, auth, tenancy, or production migration is introduced.
- Placeholder scan: no TODO/TBD implementation step is required; each step names a file, command, or concrete interface.
