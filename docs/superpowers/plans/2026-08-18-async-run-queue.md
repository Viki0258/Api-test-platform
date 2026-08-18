# Async Run Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a durable asynchronous run path with Redis Streams, MySQL job state, independent bounded workers, retries, lease recovery, and a status API while preserving the existing synchronous run API.

**Architecture:** The API validates a request, stores a transient payload in Redis, and commits a `run_jobs` row plus a `run_outbox` row in one SQL transaction. An outbox dispatcher publishes only the `run_id`, attempt, and state version to a Redis Streams consumer group. A separate Worker process claims a database job token and a Redis global-capacity lease before invoking the existing sequential `TestExecutor`; terminal results are saved through `RunHistoryStore` and status transitions are guarded by the token.

**Tech Stack:** Python 3.11, FastAPI, Pydantic Settings, SQLAlchemy Core 2.x, MySQL/SQLite, Redis Streams, `redis-py`, `fakeredis` test doubles, pytest, HTTPX, Docker Compose.

**Spec:** `docs/comet/changes/async-run-queue/specs/async-run-queue/spec.md`

## Global Constraints

- `POST /api/v1/runs` remains synchronous and keeps its existing response shape; the async path is additive at `POST /api/v1/runs/async`.
- `GET /api/v1/runs/{run_id}/status` exposes only lifecycle metadata and sanitized terminal errors; request targets, headers, query values, bodies, variables, credentials, Redis URLs, and raw driver errors never appear in status or history.
- Redis is required for the async path; missing/unavailable Redis returns stable 503 `ASYNC_RUNS_UNAVAILABLE` and never calls `TestExecutor`.
- MySQL is the durable multi-instance profile. SQLite remains a local fallback for tests/demo and must not claim cross-host queue guarantees.
- `run_jobs` and `run_outbox` use SQLAlchemy Core, parameterized transactions, stable UUID keys, and InnoDB-compatible metadata for MySQL.
- Redis Streams events contain only `run_id`, attempt, and state version. The full validated request lives in a Redis payload key with a bounded TTL and is removed after terminal handling when possible.
- `ASYNC_MAX_ACTIVE_RUNS` is the shared Redis-backed global execution bound; every worker must acquire and renew one capacity lease before invoking the executor. `ASYNC_WORKER_CONCURRENCY` is a per-process upper bound.
- Job ownership and capacity leases are renewed during execution and expire after worker failure. State-version checks make redelivery and stale worker updates harmless.
- Test assertion failures are completed test results with `passed: false`; infrastructure errors retry up to the configured maximum before entering `failed`.
- No authentication, tenant isolation, user quotas, production connections, migration execution, deployment, push, or destructive cleanup is included.

---

### Task 1: Freeze async configuration, state schemas, and dependency approval

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/app/config.py`
- Modify: `src/app/schemas.py`
- Create: `tasks/approvals/redis-async-run-queue.md`
- Test: `tests/test_configuration_wiring.py`
- Create: `tests/test_async_schemas.py`

**Interfaces:**
- `Settings.redis_url: str | None` accepts empty/None, `redis://`, and `rediss://` URLs without exposing credentials in errors.
- `Settings.async_stream_name: str`, `async_consumer_group: str`, `async_max_active_runs: int`, `async_worker_concurrency: int`, `async_job_lease_seconds: int`, `async_payload_ttl_seconds: int`, and `async_max_attempts: int` have bounded defaults.
- `RunJobState` has exactly `queued`, `running`, `completed`, and `failed`.
- `AsyncRunAccepted` contains `run_id`, `status`, `status_url`, and `created_at`.
- `AsyncRunStatus` contains `run_id`, `status`, `attempt`, `created_at`, optional `started_at`/`finished_at`, and optional sanitized `error_code`/`error_message`.

- [ ] **Step 1: Record the approved Redis dependency/service operation.**

  Create `tasks/approvals/redis-async-run-queue.md` with the operation (add the `redis` production dependency, `fakeredis` test dependency, and project-local Redis Compose service), object, reason, impact, reversibility, rollback, and the user's explicit confirmation that Redis is allowed. Do not start a Redis service or install packages globally.

- [ ] **Step 2: Write failing schema and settings tests.**

  Add tests that assert the Redis URL validator accepts `redis://` and `rediss://`, rejects HTTP/invalid schemes, default settings are bounded, `AsyncRunAccepted` serializes a UUIDv4 and status URL, and `AsyncRunStatus` rejects arbitrary lifecycle states.

- [ ] **Step 3: Run the focused tests and verify the new contract is absent.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_async_schemas.py tests/test_configuration_wiring.py -q
  ```

  Expected: failure because the async settings and schemas do not yet exist.

- [ ] **Step 4: Add the dependency declarations and settings/schemas.**

  Add `redis>=5,<6` to runtime dependencies and `fakeredis>=2.25,<3` to development dependencies. Add the settings with bounded validation and add the lifecycle/accepted/status Pydantic models without changing existing models.

- [ ] **Step 5: Rerun the focused tests.**

  Run the same pytest command and confirm it passes. Run `python -m pip check` after the dependency is installed in the project `.venv` only.

### Task 2: Implement durable job and Outbox storage

**Files:**
- Create: `src/app/services/run_queue_store.py`
- Modify: `src/app/services/run_history.py`
- Test: `tests/test_run_queue_store.py`
- Test: `tests/test_mysql_storage.py`

**Interfaces:**
- `RunQueueStore(database_url: str | None = None)` exposes `initialize()`, `create_job(run_id, payload_key)`, `get_status(run_id)`, `claim_job(run_id, owner_id, lease_seconds)`, `renew_job(run_id, owner_id, lease_seconds)`, `complete_job(run_id, owner_id)`, `schedule_retry(run_id, owner_id, error_code, error_message, next_attempt_at)`, `fail_job(run_id, owner_id, error_code, error_message)`, `pending_outbox(limit)`, and `mark_outbox_published(outbox_id)`.
- `RunQueueStore.claim_job` returns a current state/version token or `None`; all subsequent updates require that token.
- `RunQueueStore` stores only job metadata, payload key, attempts, timestamps, lease owner/version, and sanitized errors. It never stores the request JSON.

- [ ] **Step 1: Write failing storage tests.**

  Add SQLite tests for table initialization, queued status creation, conditional job claiming, stale-token rejection, retry-to-queued transitions, terminal transitions, and outbox listing/marking. Add MySQL dialect tests asserting `ENGINE=InnoDB`, stable UUID keys, and no request payload columns.

- [ ] **Step 2: Run the focused storage tests and verify failure.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_run_queue_store.py tests/test_mysql_storage.py -q
  ```

  Expected: failure because `RunQueueStore` and its metadata do not exist.

- [ ] **Step 3: Add queue metadata and transaction methods.**

  Define `run_jobs` and `run_outbox` SQLAlchemy Core tables in a focused metadata object. Implement repository-local SQLite initialization and MySQL InnoDB metadata compilation, use short transactions, and wrap SQLAlchemy/driver errors in `HistoryStorageError`-style stable queue errors.

- [ ] **Step 4: Add idempotent state transitions.**

  Implement owner/version predicates for renew, complete, retry, and fail. A duplicate terminal call must be a no-op or return the existing terminal state; a stale owner must never overwrite a newer owner.

- [ ] **Step 5: Rerun the focused storage tests.**

  Confirm SQLite behavior and MySQL DDL compilation pass without connecting to a real database.

### Task 3: Implement Redis Streams, payload TTL, and global capacity lease

**Files:**
- Create: `src/app/services/redis_run_queue.py`
- Create: `tests/test_redis_run_queue.py`

**Interfaces:**
- `RedisRunQueue(redis_url, stream_name, consumer_group)` exposes `ping()`, `ensure_group()`, `put_payload(payload_key, payload_json, ttl_seconds)`, `get_payload(payload_key)`, `delete_payload(payload_key)`, `publish(run_id, attempt, state_version)`, `read(consumer_name, count, block_ms)`, `claim_expired(consumer_name, min_idle_ms, count)`, and `ack(message_id)`.
- `RedisRunQueue.acquire_capacity(owner_id, run_id, limit, lease_seconds)` returns a `RedisCapacityLease` or `None`; the lease exposes `renew()` and `release()`.
- Capacity acquisition, expired-token cleanup, and insertion are atomic. All workers sharing Redis and the same limit observe one global active-run bound.

- [ ] **Step 1: Write failing Redis tests with `fakeredis`.**

  Cover group initialization idempotency, payload TTL/deletion, stream event fields containing only `run_id`/attempt/state version, global capacity rejection, release, renewal, expired lease reclamation, and pending-message acknowledgement/claim behavior.

- [ ] **Step 2: Run the focused tests and verify failure.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_redis_run_queue.py -q
  ```

  Expected: failure because the Redis adapter does not exist.

- [ ] **Step 3: Implement the Redis adapter and atomic capacity scripts.**

  Use `redis.Redis.from_url(..., decode_responses=True)`. Use `XGROUP CREATE ... MKSTREAM`, `XADD`, `XREADGROUP`, `XAUTOCLAIM`, and `XACK`. Implement the capacity lease as a sorted set with a Lua acquire/renew/release script so expired members are removed and the global limit is checked atomically.

- [ ] **Step 4: Wrap connection and command failures.**

  Convert Redis connection/command errors into a stable `AsyncRunQueueUnavailable` without returning the URL, password, host, or raw Redis exception.

- [ ] **Step 5: Rerun the focused Redis tests.**

  Confirm all queue, TTL, lease, and pending-message tests pass.

### Task 4: Add async submission and status APIs

**Files:**
- Create: `src/app/services/async_run_service.py`
- Modify: `src/app/main.py`
- Test: `tests/test_async_run_api.py`

**Interfaces:**
- `AsyncRunService.submit(payload: TestRunRequest) -> AsyncRunAccepted` validates the same target policy, writes the transient Redis payload, commits one job plus one outbox event, and cleans up the payload on transaction failure.
- `AsyncRunService.status(run_id: UUID) -> AsyncRunStatus` returns only safe job metadata.
- `POST /api/v1/runs/async` returns 202 and never constructs or calls `TestExecutor`.
- `GET /api/v1/runs/{run_id}/status` returns queued/running/terminal status and maps missing ids to the existing safe 404 shape.

- [ ] **Step 1: Write failing API tests.**

  Test 202 acceptance, UUID/status URL shape, no executor call, invalid payload/target rejection before Redis, stable 503 when Redis is unavailable, rollback cleanup when job creation fails, status transitions, and absence of request data from status responses. Keep existing synchronous API tests unchanged.

- [ ] **Step 2: Run the focused API tests and verify failure.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_async_run_api.py -q
  ```

  Expected: failure because the async routes and service are absent.

- [ ] **Step 3: Wire dependency factories without reading secrets.**

  Add cached settings-based factories for `RunQueueStore` and `RedisRunQueue`; retain dependency overrides used by existing tests. Ensure the Redis health check occurs before creating an accepted job.

- [ ] **Step 4: Implement submit/status behavior.**

  Generate the run UUID at submission, serialize the request only to the Redis payload key, create the durable job/outbox transaction, and return the additive 202/status response. Map queue failures to `ASYNC_RUNS_UNAVAILABLE` and storage failures to a stable 503.

- [ ] **Step 5: Rerun the focused API tests and the existing API suite.**

  Confirm the new async tests and all existing run/history tests pass together.

### Task 5: Implement dispatcher and bounded Worker execution

**Files:**
- Create: `src/app/services/async_run_worker.py`
- Create: `src/app/worker.py`
- Test: `tests/test_async_run_worker.py`

**Interfaces:**
- `AsyncRunWorker.run_once() -> bool` dispatches pending outbox entries, consumes or reclaims one stream message, and performs at most one execution transition.
- `AsyncRunWorker.run_forever()` loops with bounded blocking reads and graceful shutdown.
- `python -m app.worker` starts the dispatcher/consumer using settings and never starts FastAPI.

- [ ] **Step 1: Write failing worker tests.**

  Test that a worker claims one job token and one global capacity lease before execution, does not exceed local/global bounds, loads and deletes the transient payload, saves one redacted result, acknowledges success, classifies assertion failures as completed, retries infrastructure errors with bounded attempts, and refuses stale-token updates.

- [ ] **Step 2: Run the focused worker tests and verify failure.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_async_run_worker.py -q
  ```

  Expected: failure because the worker service and entry point are absent.

- [ ] **Step 3: Implement the single-message worker path.**

  Dispatch pending outbox records, acquire the global Redis capacity lease, claim the database job token, load the payload, invoke the existing `TestExecutor` with the existing sequential semantics, save the result, and perform token/lease/payload/message cleanup in terminal paths.

- [ ] **Step 4: Implement heartbeats, retry, and recovery.**

  Renew the job token and Redis capacity lease during execution. On infrastructure failure, schedule a bounded retry through a new outbox event; after the maximum attempt count, mark failed with sanitized code/message. Use `XAUTOCLAIM` for expired pending entries and ensure stale workers cannot update the job.

- [ ] **Step 5: Rerun worker tests and compile the entry point.**

  Run the focused worker suite and:

  ```powershell
  .\.venv\Scripts\python.exe -m py_compile src/app/worker.py src/app/services/async_run_worker.py
  ```

### Task 6: Add local Redis operations and documentation

**Files:**
- Create: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_workspace_tools.py` only if workspace validation needs an explicit Compose/config assertion

- [ ] **Step 1: Add a project-local Redis Compose service.**

  Define one Redis service with a pinned major image, a named project-local volume, no host-wide service changes, and no application secret in the file. Keep the service optional; tests must not require Docker to be running.

- [ ] **Step 2: Document async submission and worker startup.**

  Document `REDIS_URL`, queue settings, `POST /api/v1/runs/async`, status polling, `python -m app.worker`, retry/lease behavior, Redis ACL/TLS expectations, and the distinction between synchronous compatibility and asynchronous production execution.

- [ ] **Step 3: Add a sanitized PowerShell demo.**

  Show how to start the project-local Redis service, launch API and worker as separate local processes, submit a run, poll status, and fetch the existing completed result without embedding credentials.

- [ ] **Step 4: Run workspace/documentation checks.**

  Run `python .\scripts\validate_workspace.py` and inspect the diff for secrets, external paths, unapproved services, and accidental changes outside the repository.

### Task 7: Full verification and Native Builder handoff

**Files:**
- Modify: `tasks/active/async-run-queue.md`
- Modify: `docs/comet/changes/async-run-queue/brief.md` only if implementation evidence requires a clarified known limit
- Modify: `docs/comet/changes/async-run-queue/comet-state.yaml` only through Runtime
- Create: `tmp/native-builder-handoff-async-run-queue.json` temporarily, then delete it after Runtime accepts it

- [ ] **Step 1: Run the complete quality gates.**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
  .\.venv\Scripts\python.exe -m compileall -q src tests scripts
  node --check frontend/app.js
  .\.venv\Scripts\python.exe -m pip check
  .\.venv\Scripts\python.exe .\scripts\validate_workspace.py
  git diff --check
  ```

- [ ] **Step 2: Inspect final diff and approval state.**

  Confirm only the async queue, Redis dependency/Compose, tests, docs, task card, and Native artifacts changed. Confirm no `.env`, real endpoint, credential, production database, or external filesystem was read or written.

- [ ] **Step 3: Submit Builder handoff to Runtime.**

  Write a temporary JSON file using the Runtime-provided `builder-handoff` template. Include the exact addressed acceptance IDs, every executed check and result, and known limits such as unverified real Redis/MySQL connectivity, no authentication/tenant isolation, and no production deployment. Run the exact `comet native next async-run-queue --runner-input ...` continuation.

- [ ] **Step 4: Follow the Runtime verifier continuation.**

  If Runtime dispatches checks, run only its commands, submit results through the required JSON bridge, and do not claim completion until the verifier covers every acceptance item.

- [ ] **Step 5: Finish the project task gate.**

  After Native verification and user confirmation, run `python .\scripts\complete_task.py async-run-queue --check`; then run it without `--check` to move the task card to `tasks/done`. Do not push or deploy.

## Plan self-review

- **Spec coverage:** A1-A12 map to Tasks 1-6; the scenario-level requirements map to Tasks 2-5 and the final Native handoff.
- **Type consistency:** API schemas use `RunJobState`; storage returns `AsyncRunStatus`; worker consumes the `RunQueueStore` token/version and `RedisCapacityLease` interfaces defined above.
- **Failure coverage:** Redis unavailability, transaction rollback, outbox publication failure, duplicate delivery, worker crash, lease expiry, stale updates, retry exhaustion, and assertion failures each have a named test step.
- **Security coverage:** Request payloads are transient Redis data with TTL; history/status remain redacted; credentials and raw driver errors are excluded.
- **Scope:** No authentication, tenancy, billing, production migration, deployment, or breaking synchronous API change is included.
- **Self-review scan:** Completed; each step names files, interfaces, commands, and expected outcomes without deferred implementation wording.
