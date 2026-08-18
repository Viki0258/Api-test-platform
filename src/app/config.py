from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "API Test Platform"
    request_timeout_seconds: float = 10.0
    run_budget_seconds: float = 30.0
    database_url: str | None = None
    max_concurrent_runs: int = Field(default=4, ge=1, le=64)
    redis_url: str | None = None
    async_stream_name: str = Field(
        default="api-test-runs",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    async_consumer_group: str = Field(
        default="api-test-workers",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
    )
    async_max_active_runs: int = Field(default=16, ge=1, le=1024)
    async_worker_concurrency: int = Field(default=4, ge=1, le=64)
    async_job_lease_seconds: int = Field(default=60, ge=5, le=3600)
    async_payload_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
    )
    async_max_attempts: int = Field(default=3, ge=1, le=10)
    allowed_target_origins: str = ""
    allow_local_targets: bool = False
    ai_provider: str = "mock"
    ai_request_timeout_seconds: float = 30.0
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-terra"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator("ai_provider")
    @classmethod
    def validate_ai_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"mock", "openai"}:
            raise ValueError("ai_provider must be 'mock' or 'openai'")
        return normalized

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None

        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"sqlite", "mysql+pymysql"}:
            raise ValueError(
                "database_url must use sqlite or mysql+pymysql"
            )
        if parsed.username is not None and parsed.username.strip() == "":
            raise ValueError("database_url must include a valid username")
        if parsed.password is not None and parsed.password.strip() == "":
            raise ValueError("database_url must include a valid password")
        if parsed.scheme == "mysql+pymysql" and not parsed.hostname:
            raise ValueError("mysql database_url must include a host")
        if parsed.fragment:
            raise ValueError("database_url must not include a fragment")
        return normalized

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None

        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("redis_url must use redis or rediss with a host")
        if parsed.fragment:
            raise ValueError("redis_url must not include a fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("redis_url must include a valid port") from exc
        return normalized

    def target_origins(self) -> frozenset[str]:
        return frozenset(
            origin
            for value in self.allowed_target_origins.split(",")
            if (origin := normalize_origin(value.strip(), origin_only=True))
        )


def normalize_origin(value: str, *, origin_only: bool = True) -> str | None:
    """Return a canonical HTTP origin or ``None`` for malformed input."""
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (origin_only and parsed.path not in {"", "/"})
        or parsed.query
        or parsed.fragment
    ):
        return None

    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    host = parsed.hostname.lower()
    display_host = f"[{host}]" if ":" in host else host
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{display_host}{port_suffix}"


def target_is_allowed(base_url: str, settings: Settings) -> bool:
    origin = normalize_origin(base_url, origin_only=False)
    if origin is None:
        return False
    if origin in settings.target_origins():
        return True

    parsed = urlsplit(origin)
    return settings.allow_local_targets and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
