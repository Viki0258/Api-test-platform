from pathlib import Path
from functools import lru_cache
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings, target_is_allowed
from app.schemas import (
    OpenApiGenerateRequest,
    OpenApiGenerateResponse,
    TestRunHistoryList,
    TestRunRequest,
    TestRunResult,
)
from app.services.executor import TestExecutor
from app.services.openapi_generator import (
    OpenApiGenerationError,
    generate_openapi_cases,
)
from app.services.run_history import HistoryStorageError, RunHistoryStore
from app.services.report_renderer import (
    REPORT_CONTENT_SECURITY_POLICY,
    render_test_run_report,
)

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


@app.post(
    "/api/v1/openapi/generate",
    response_model=OpenApiGenerateResponse,
    tags=["case-generation"],
)
def generate_from_openapi(
    payload: OpenApiGenerateRequest,
) -> OpenApiGenerateResponse:
    try:
        return generate_openapi_cases(payload)
    except OpenApiGenerationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from None


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
    parsed_run_id = _parse_run_id(run_id)
    return _get_stored_run(parsed_run_id, history_store)


@app.get(
    "/api/v1/runs/{run_id}/report",
    response_class=HTMLResponse,
    tags=["test-runs"],
)
def get_test_run_report(
    run_id: str,
    history_store: RunHistoryStore = Depends(get_run_history_store),
) -> HTMLResponse:
    parsed_run_id = _parse_run_id(run_id)
    result = _get_stored_run(parsed_run_id, history_store)
    return HTMLResponse(
        content=render_test_run_report(result),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": REPORT_CONTENT_SECURITY_POLICY,
            "Content-Disposition": (
                f'attachment; filename="api-test-report-'
                f'{parsed_run_id}.html"'
            ),
        },
    )


def _parse_run_id(run_id: str) -> UUID:
    try:
        parsed_run_id = UUID(run_id)
        if parsed_run_id.version != 4:
            raise ValueError
    except (ValueError, AttributeError):
        raise _run_not_found() from None
    return parsed_run_id


def _get_stored_run(
    run_id: UUID,
    history_store: RunHistoryStore,
) -> TestRunResult:
    try:
        result = history_store.get(run_id)
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
