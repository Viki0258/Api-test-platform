# Async Run Queue Design

## Classification

This is an architectural change. It changes the execution boundary from an
HTTP handler to a durable queue and independent workers, while preserving the
existing synchronous API for compatibility.

## Goal

Provide a normal platform execution path for long-running API test runs:
clients submit a task, receive a `run_id`, workers execute with bounded
concurrency, and MySQL records durable state and the already-redacted result.

## Alternatives considered

### Redis Streams with a direct Python worker (selected)

Redis Streams consumer groups provide acknowledgement, pending entries and
worker recovery. `redis-py` keeps the implementation explicit and avoids a
second task framework. A MySQL job table and outbox make task state durable and
cover the failure window between the API transaction and Redis publication.

### Celery/RQ with Redis

This would shorten the initial worker implementation and provide established
retry primitives, but adds a framework-specific task model, more configuration,
and less control over the existing SQLAlchemy result lifecycle. It is a good
future option if the task catalog grows beyond run execution.

### MySQL-backed queue without Redis

MySQL 8 row locking and `SKIP LOCKED` could implement a queue without a new
service. It would simplify deployment but turns the primary database into a
high-frequency broker, increases lock contention, and provides weaker
backpressure and recovery semantics. It is not the recommended platform path.

## Chosen architecture

The API validates the request and target using the existing rules. It writes a
`run_jobs` row and a `run_outbox` row in one MySQL transaction. The complete
execution request is kept only in a Redis key with a finite TTL; the outbox
event contains the run id, attempt and state version rather than credentials or
request data. A dispatcher publishes the event to a Redis Streams consumer
group.

Workers run as a separate process entry point. Each worker has a local bounded
executor pool and consumes the shared stream. Before executing, it atomically
claims the job by changing its state/version and setting a lease expiry. Only
the current owner may persist a result or transition the task. After terminal
handling, the worker acknowledges the stream entry and removes the temporary
payload when possible. Expired pending entries are reclaimed by another worker;
attempt limits prevent infinite retry loops.

The existing synchronous endpoint remains unchanged. The async endpoint and
status endpoint are additive, so existing clients can migrate gradually.

## Data model

`run_jobs` stores `run_id`, state, attempt count, state version, creation/start/
finish timestamps, lease expiry, and sanitized terminal error fields. It never
stores the request target, headers, query values, body, variables, or secrets.

`run_outbox` stores a durable event id, run id, event kind, publication state,
attempt count, next retry time and sanitized publication error. It is consumed
by the dispatcher and retained only according to an explicit cleanup policy.

`test_runs` continues to store the existing redacted `TestRunResult`; it remains
the source for completed result and report endpoints.

## Public behavior

`POST /api/v1/runs/async` returns:

```json
{
  "run_id": "<uuidv4>",
  "status": "queued",
  "status_url": "/api/v1/runs/<uuidv4>/status"
}
```

`GET /api/v1/runs/{run_id}/status` reports lifecycle state without exposing
request data. Existing `GET /api/v1/runs/{run_id}` continues to return a
completed result only; callers use the status endpoint while a task is queued
or running.

## Failure and recovery

- Redis unavailable at submission returns a stable 503 and never calls the
  executor.
- Outbox publication is retryable; a committed job is not silently lost.
- A worker lease expiry allows pending work to be claimed by another worker.
- State version checks prevent stale workers from overwriting a newer owner.
- Test assertion failures are valid completed results, not infrastructure
  retries.
- Infrastructure failures retry with a bounded policy and a sanitized terminal
  error.
- Duplicate delivery is safe because job claiming and history persistence are
  keyed by the UUID run id and current state version.

## Security and deployment boundaries

Redis is an execution transport and transient payload store, not run history.
Deployments must protect it with ACL/TLS and an appropriate persistence/TTL
policy. This change does not add authentication or tenant isolation; those are
separate prerequisites before exposing the service to untrusted users.

Local development may use SQLite for job/history tests and Docker Compose for
Redis. Cross-host durability and global coordination are claimed only for the
MySQL + Redis deployment profile. Production connections, migrations and
deployment remain outside local verification.

## Verification plan

The implementation will add tests for asynchronous acceptance, unavailable
Redis, job state transitions, outbox retry, duplicate delivery, lease recovery,
bounded worker concurrency, retry exhaustion, result redaction and synchronous
compatibility. The full project quality gates remain mandatory, including
branch coverage of at least 90%, compilation, dependency checks, workspace
validation and whitespace validation.

## Scope guard

Authentication, tenant isolation, user quotas, billing, multi-region routing,
and production infrastructure changes are explicitly excluded. They should be
separate changes with their own data model and approval boundaries.
