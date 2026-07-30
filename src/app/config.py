from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "API Test Platform"
    request_timeout_seconds: float = 10.0
    run_budget_seconds: float = 30.0
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
    )

    @field_validator("ai_provider")
    @classmethod
    def validate_ai_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"mock", "openai"}:
            raise ValueError("ai_provider must be 'mock' or 'openai'")
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
