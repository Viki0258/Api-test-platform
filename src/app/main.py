from pathlib import Path
from functools import lru_cache
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings, target_is_allowed
from app.schemas import (
    AiCandidateEvaluationRequest,
    AiDraftEvaluationRequest,
    AiEvaluationResponse,
    AiGenerateRequest,
    AiGenerateResponse,
    AiProviderStatus,
    AsyncRunAccepted,
    AsyncRunStatus,
    OpenApiGenerateRequest,
    OpenApiGenerateResponse,
    TestRunHistoryList,
    TestRunRequest,
    TestRunResult,
)
from app.services.ai_assistant import AiAssistantError, AiAssistantService
from app.services.async_run_service import (
    AsyncRunService,
    AsyncRunServiceError,
)
from app.services.concurrency import RunCapacityLimiter
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
from app.services.redis_run_queue import RedisRunQueue
from app.services.run_queue_store import RunQueueStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIRECTORY = REPOSITORY_ROOT / "frontend"
AI_EVALUATION_PATHS = {
    "/api/v1/ai/cases/evaluate",
    "/api/v1/ai/drafts/evaluate",
}
AI_EVALUATION_ERROR_MESSAGE = "AI evaluation request failed"
AI_EVALUATION_RESPONSES = {
    422: {
        "description": "Invalid AI evaluation input.",
        "content": {
            "application/json": {
                "examples": {
                    "invalid_input": {
                        "value": {
                            "detail": {
                                "code": "INVALID_AI_EVALUATION_INPUT",
                                "message": AI_EVALUATION_ERROR_MESSAGE,
                            }
                        }
                    },
                    "source_too_large": {
                        "value": {
                            "detail": {
                                "code": "AI_EVALUATION_SOURCE_TOO_LARGE",
                                "message": AI_EVALUATION_ERROR_MESSAGE,
                            }
                        }
                    }
                }
            }
        },
    },
    502: {
        "description": "AI provider is unavailable or returned invalid output.",
        "content": {
            "application/json": {
                "examples": {
                    "provider_unavailable": {
                        "value": {
                            "detail": {
                                "code": "AI_PROVIDER_UNAVAILABLE",
                                "message": AI_EVALUATION_ERROR_MESSAGE,
                            }
                        }
                    },
                    "invalid_output": {
                        "value": {
                            "detail": {
                                "code": "AI_PROVIDER_INVALID_OUTPUT",
                                "message": AI_EVALUATION_ERROR_MESSAGE,
                            }
                        }
                    },
                }
            }
        },
    },
    503: {
        "description": "AI provider is not configured.",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": "AI_PROVIDER_NOT_CONFIGURED",
                        "message": AI_EVALUATION_ERROR_MESSAGE,
                    }
                }
            }
        },
    },
}

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


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
):
    if request.url.path in AI_EVALUATION_PATHS:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "INVALID_AI_EVALUATION_INPUT",
                    "message": AI_EVALUATION_ERROR_MESSAGE,
                }
            },
        )
    return await request_validation_exception_handler(request, exc)


@lru_cache
def _build_run_history_store(database_url: str | None) -> RunHistoryStore:
    return RunHistoryStore(database_url=database_url)


def get_run_history_store(
    settings: Settings = Depends(get_settings),
) -> RunHistoryStore:
    return _build_run_history_store(settings.database_url)


@lru_cache
def _build_run_queue_store(database_url: str | None) -> RunQueueStore:
    return RunQueueStore(database_url=database_url)


def get_run_queue_store(
    settings: Settings = Depends(get_settings),
) -> RunQueueStore:
    return _build_run_queue_store(settings.database_url)


@lru_cache
def _build_redis_run_queue(
    redis_url: str | None,
    stream_name: str,
    consumer_group: str,
) -> RedisRunQueue:
    return RedisRunQueue(redis_url, stream_name, consumer_group)


def get_redis_run_queue(
    settings: Settings = Depends(get_settings),
) -> RedisRunQueue:
    return _build_redis_run_queue(
        settings.redis_url,
        settings.async_stream_name,
        settings.async_consumer_group,
    )


def get_async_run_service(
    settings: Settings = Depends(get_settings),
    queue: RedisRunQueue = Depends(get_redis_run_queue),
    store: RunQueueStore = Depends(get_run_queue_store),
) -> AsyncRunService:
    return AsyncRunService(settings, queue, store)


@lru_cache
def get_run_capacity_limiter() -> RunCapacityLimiter:
    return RunCapacityLimiter(get_settings().max_concurrent_runs)


def get_ai_assistant(
    settings: Settings = Depends(get_settings),
) -> AiAssistantService:
    try:
        return AiAssistantService(settings)
    except AiAssistantError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


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


@app.get(
    "/api/v1/ai/status",
    response_model=AiProviderStatus,
    tags=["ai-assistant"],
)
def get_ai_status(
    assistant: AiAssistantService = Depends(get_ai_assistant),
) -> AiProviderStatus:
    return assistant.status()


@app.post(
    "/api/v1/ai/cases/generate",
    response_model=AiGenerateResponse,
    tags=["ai-assistant"],
)
def generate_ai_cases(
    payload: AiGenerateRequest,
    assistant: AiAssistantService = Depends(get_ai_assistant),
) -> AiGenerateResponse:
    try:
        return assistant.generate(payload)
    except AiAssistantError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


@app.post(
    "/api/v1/ai/cases/evaluate",
    response_model=AiEvaluationResponse,
    tags=["ai-assistant", "ai-evaluation"],
    responses=AI_EVALUATION_RESPONSES,
)
def evaluate_ai_candidates(
    payload: AiCandidateEvaluationRequest,
    assistant: AiAssistantService = Depends(get_ai_assistant),
) -> AiEvaluationResponse:
    try:
        return assistant.evaluate_candidates(payload)
    except AiAssistantError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": AI_EVALUATION_ERROR_MESSAGE,
            },
        ) from None


@app.post(
    "/api/v1/ai/drafts/evaluate",
    response_model=AiEvaluationResponse,
    tags=["ai-assistant", "ai-evaluation"],
    responses=AI_EVALUATION_RESPONSES,
)
def evaluate_ai_draft(
    payload: AiDraftEvaluationRequest,
    assistant: AiAssistantService = Depends(get_ai_assistant),
) -> AiEvaluationResponse:
    try:
        return assistant.evaluate_draft(payload)
    except AiAssistantError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": AI_EVALUATION_ERROR_MESSAGE,
            },
        ) from None


@app.post("/api/v1/runs", response_model=TestRunResult, tags=["test-runs"])
def create_test_run(
    payload: TestRunRequest,
    settings: Settings = Depends(get_settings),
    history_store: RunHistoryStore = Depends(get_run_history_store),
    capacity_limiter: RunCapacityLimiter = Depends(get_run_capacity_limiter),
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
    if not capacity_limiter.try_acquire():
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "1"},
            detail={
                "code": "RUN_CAPACITY_EXCEEDED",
                "message": "run capacity is temporarily exhausted",
            },
        )

    try:
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
    finally:
        capacity_limiter.release()


@app.post(
    "/api/v1/runs/async",
    response_model=AsyncRunAccepted,
    status_code=202,
    tags=["test-runs"],
)
def create_async_test_run(
    payload: TestRunRequest,
    service: AsyncRunService = Depends(get_async_run_service),
) -> AsyncRunAccepted:
    try:
        return service.submit(payload)
    except AsyncRunServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


@app.get(
    "/api/v1/runs/{run_id}/status",
    response_model=AsyncRunStatus,
    tags=["test-runs"],
)
def get_async_run_status(
    run_id: str,
    service: AsyncRunService = Depends(get_async_run_service),
) -> AsyncRunStatus:
    parsed_run_id = _parse_run_id(run_id)
    try:
        return service.status(parsed_run_id)
    except AsyncRunServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None


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
