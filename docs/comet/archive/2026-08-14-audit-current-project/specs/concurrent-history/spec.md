# Concurrent run history

The service keeps the existing synchronous request/response contract for one
run while allowing independent client requests to execute at the same time.

- DATABASE_URL is optional. With no value, the default SQLite database remains
  .data/run-history.sqlite3 and is restricted to the repository. With a
  mysql+pymysql URL, the store uses a MySQL InnoDB table with a stable
  sequence tie-breaker and a LONGTEXT/JSON-compatible result column.
- The storage interface exposes initialize(), save(result), list(limit), and
  get(run_id); callers do not depend on a database driver.
- The persisted payload is the already redacted TestRunResult. Request target,
  headers, query values, request body, run variables, credentials, and raw
  driver errors are never persisted or returned.
- MAX_CONCURRENT_RUNS defaults to 4 and is a bounded positive setting. A
  request acquires one slot only after payload and target validation. If no
  slot is immediately available, the API returns 429 with detail code
  RUN_CAPACITY_EXCEEDED and a positive Retry-After header; it does not call
  the executor or history store.
- A slot is released in a finally block after execution and history save,
  including executor exceptions and persistence failures. There is no queue.
- Existing run-history routes and safe response fields remain compatible. The
  run executor remains sequential within a run, while independent HTTP client
  requests may overlap.

## Scenarios

### Scenario: SQLite fallback remains available

Given DATABASE_URL is unset
When a run is saved and then listed and loaded
Then the result is stored in the repository-local SQLite database with the
existing summary ordering and response shape.

### Scenario: MySQL URL selects the MySQL dialect

Given DATABASE_URL has the mysql+pymysql scheme
When the history engine is created
Then SQLAlchemy uses the MySQL dialect with an InnoDB-compatible table definition
and does not expose the URL or credentials in storage errors.

### Scenario: Concurrent runs are isolated

Given multiple clients submit valid runs concurrently
When each run completes
Then each result has its own run id, variables and cases, and no result contains
state from another run.

### Scenario: Capacity is bounded

Given all configured run slots are occupied
When another valid run is submitted
Then the request returns 429 with RUN_CAPACITY_EXCEEDED, a positive
Retry-After, and no executor or persistence call.

### Scenario: Capacity is released after every terminal path

Given a run finishes successfully, fails during execution, or fails while saving history
When the next valid run is submitted
Then the next run can acquire the slot and the prior terminal error remains
sanitized and stable.

### Scenario: Concurrent history writes are consistent

Given several completed results are saved at the same time
When history is listed and individual details are loaded
Then every retained result is complete, unique, readable, and ordered newest first.
