from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings, target_is_allowed
from app.schemas import TestRunRequest, TestRunResult
from app.services.executor import TestExecutor

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
    return executor.run(
        base_url,
        payload.cases,
        variables=payload.variables,
        secret_variables=payload.secret_variables,
    )
