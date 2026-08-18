# Async run queue

The platform keeps the existing synchronous run API and adds a durable
asynchronous execution path backed by Redis Streams and MySQL job state.

## Requirements

- `POST /api/v1/runs/async` accepts the existing `TestRunRequest` after the
  same payload and target validation. It returns HTTP 202 with a UUIDv4
  `run_id`, `status: queued`, and a status URL. It never executes the target
  request in the API handler.
- `GET /api/v1/runs/{run_id}/status` returns a safe status document containing
  only the run id, state, attempt count, timestamps, and sanitized terminal
  error information. A queued or running job is not represented as a completed
  `TestRunResult`.
- The persistent job state has the states `queued`, `running`, `completed`, and
  `failed`. A test run whose assertions fail is `completed` with a result whose
  `passed` is false. Infrastructure failures become `failed` only after retry
  policy is exhausted.
- MySQL persists `run_jobs` and `run_outbox` in InnoDB tables. SQLite remains a
  local test/demo fallback, but the service does not claim cross-host queue
  guarantees for SQLite.
- The API transaction creates one job and one outbox record. A dispatcher
  publishes a stream entry containing only `run_id`, attempt, and state
  version. A temporary Redis payload key holds the validated execution request
  with a finite TTL and is deleted on terminal completion when possible.
- Workers consume one Redis Streams consumer group. A worker must acquire a
  database-backed execution token/version before invoking `TestExecutor`.
  Updates with a stale token are ignored, making redelivery idempotent.
- Workers acknowledge successful terminal handling. Transient infrastructure
  errors are retried with bounded attempts; unacknowledged messages whose
  execution lease expired can be claimed by another worker. A permanently
  missing or expired payload is a sanitized terminal failure.
- The aggregate number of active asynchronous executions is bounded by worker
  configuration and job lease ownership, rather than by an API-process
  semaphore. A worker must not start work beyond its configured local bound.
- Existing `POST /api/v1/runs`, run history routes, redaction behavior, SSRF
  policy, and sequential dependency execution remain compatible.
- Redis connection strings, MySQL credentials, request targets, headers,
  query values, bodies, variables, and raw driver errors must not be written to
  run history or returned by status/error APIs.

## Scenarios

### Scenario: asynchronous submission is accepted

Given Redis is configured and available
And the request passes payload and target validation
When a client posts to `/api/v1/runs/async`
Then the API returns 202 with a new run id and queued status
And no target request has been made
And the job and outbox records are durable.

### Scenario: queue dependency is unavailable

Given Redis is missing or unavailable
When a client posts a valid asynchronous run
Then the API returns 503 with `ASYNC_RUNS_UNAVAILABLE`
And it does not invoke `TestExecutor`
And it does not expose the Redis URL or driver error.

### Scenario: outbox publication is retried

Given a job and outbox record were committed
And the first Redis publication fails
When the dispatcher retries the outbox entry
Then exactly one logical run event becomes available to the consumer group
And the job remains recoverable until publication succeeds or a safe terminal error is recorded.

### Scenario: worker completes a run

Given a queued job has a non-expired payload
When a worker claims its execution token and runs the executor
Then the status becomes completed
And the already redacted result is saved once in history
And the stream entry is acknowledged
And the payload key is removed or left only until its TTL.

### Scenario: duplicate delivery is harmless

Given the same stream event is delivered to two workers
When both attempt to claim the same job
Then only one obtains the current execution token
And the other performs no target request
And history contains at most one result for the run id.

### Scenario: crashed worker is recovered

Given a worker owns a running job and stops before acknowledgement
When the execution lease expires and another worker claims the pending entry
Then the job is retried only within the configured attempt limit
And stale status updates from the old worker cannot overwrite the new owner.

### Scenario: test assertion failure is a completed result

Given the target requests finish but one or more assertions fail
When the worker persists the result
Then the job status is completed
And the result has `passed: false`
And the API does not treat the test outcome as infrastructure failure.

### Scenario: synchronous compatibility remains

Given a client uses the existing synchronous endpoint
When it submits a valid run
Then its request/response shape and history behavior remain unchanged
And cases within the run still execute in dependency order.

### Scenario: sensitive request data is bounded

Given an asynchronous request contains headers, request bodies, or secret variables
When the job is queued and completed
Then those values are absent from `test_runs` and status responses
And the temporary payload is subject to its configured TTL.
