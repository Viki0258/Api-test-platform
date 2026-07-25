from pathlib import Path
from functools import lru_cache
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings, target_is_allowed
from app.schemas import (
    TestRunHistoryList,
    TestRunRequest,
    TestRunResult,
)
from app.services.executor import TestExecutor
from app.services.run_history import HistoryStorageError, RunHistoryStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIRECTORY = REPOSITORY_ROOT / "frontend"

app = FastAPI(
    title="API Test Platform",
    version="0.1.0",
    description="Execute deterministic API test cases with structured assertions.",
)
app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_DIRECTORY, check_dir=False),
    name="static",
)


@lru_cache
def get_run_history_store() -> RunHistoryStore:
    return RunHistoryStore()


@app.get("/", include_in_schema=False, response_class=FileResponse)
def visual_console() -> FileResponse:
    return FileResponse(FRONTEND_DIRECTORY / "index.html")


@app.get("/api/v1/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/demo/users/{user_id}", tags=["demo"])
def get_demo_user(user_id: int) -> dict:
    return {
        "code": 0,
        "data": {
            "id": user_id,
            "name": "demo-user",
            "active": True,
        },
    }


@app.post("/api/v1/runs", response_model=TestRunResult, tags=["test-runs"])
def create_test_run(
    payload: TestRunRequest,
    settings: Settings = Depends(get_settings),
    history_store: RunHistoryStore = Depends(get_run_history_store),
) -> TestRunResult:
    base_url = str(payload.base_url)
    if not target_is_allowed(base_url, settings):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TARGET_NOT_ALLOWED",
                "message": "base_url origin is not allowed",
            },
        )
    executor = TestExecutor(
        timeout_seconds=settings.request_timeout_seconds,
        run_budget_seconds=settings.run_budget_seconds,
    )
    result = executor.run(
        base_url,
        payload.cases,
        variables=payload.variables,
        secret_variables=payload.secret_variables,
    )
    try:
        history_store.save(result)
    except HistoryStorageError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HISTORY_PERSISTENCE_FAILED",
                "message": "test run completed but history could not be saved",
            },
        ) from None
    return result


@app.get(
    "/api/v1/runs",
    response_model=TestRunHistoryList,
    tags=["test-runs"],
)
def list_test_runs(
    limit: int = Query(default=20, ge=1, le=100),
    history_store: RunHistoryStore = Depends(get_run_history_store),
) -> TestRunHistoryList:
    try:
        items, total = history_store.list(limit)
    except HistoryStorageError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HISTORY_STORAGE_UNAVAILABLE",
                "message": "run history is temporarily unavailable",
            },
        ) from None
    return TestRunHistoryList(items=items, limit=limit, total=total)


@app.get(
    "/api/v1/runs/{run_id}",
    response_model=TestRunResult,
    tags=["test-runs"],
)
def get_test_run(
    run_id: str,
    history_store: RunHistoryStore = Depends(get_run_history_store),
) -> TestRunResult:
    try:
        parsed_run_id = UUID(run_id)
        if parsed_run_id.version != 4:
            raise ValueError
    except (ValueError, AttributeError):
        raise _run_not_found() from None

    try:
        result = history_store.get(parsed_run_id)
    except HistoryStorageError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "HISTORY_STORAGE_UNAVAILABLE",
                "message": "run history is temporarily unavailable",
            },
        ) from None
    if result is None:
        raise _run_not_found()
    return result


def _run_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "RUN_NOT_FOUND",
            "message": "test run was not found",
        },
    )
