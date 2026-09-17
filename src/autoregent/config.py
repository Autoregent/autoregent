from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutoregentConfig(BaseModel):
    """Plain, directly-constructible configuration -- no hidden singleton, no
    module-level state. Construct it however you like:

        config = AutoregentConfig(gemini_api_key="...", upstream_base_url="https://api.internal")

    or load it from the environment / a .env file with `AutoregentConfig.from_env()`,
    which is what the CLI does.
    """

    upstream_base_url: str = "http://localhost:8000"
    upstream_timeout_seconds: float = 5.0

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-flash-lite-latest"
    gemini_timeout_seconds: float = 3.0
    confidence_threshold: float = 0.85

    log_level: str = "INFO"

    max_heals_per_transaction: int = 2
    rolling_window_seconds: float = 60.0
    rolling_window_max_heals: int = 5
    circuit_cooldown_seconds: float = 30.0

    # HMAC proves integrity, not non-repudiation -- a real deployment needs
    # asymmetric signing into WORM storage. Change this for any real deployment.
    hmac_secret: str = "dev-secret-change-me-in-production"

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "AutoregentConfig":
        """Load from environment variables / a .env file, using the same field
        names uppercased (e.g. GEMINI_API_KEY). This is a thin bridge into
        pydantic-settings purely for env-var loading -- the resulting object
        is a plain AutoregentConfig, not a settings singleton."""

        class _EnvLoader(BaseSettings):
            model_config = SettingsConfigDict(env_file=env_file, env_file_encoding="utf-8", extra="ignore")

            upstream_base_url: str = "http://localhost:8000"
            upstream_timeout_seconds: float = 5.0
            gemini_api_key: str | None = None
            gemini_model: str = "gemini-flash-lite-latest"
            gemini_timeout_seconds: float = 3.0
            confidence_threshold: float = 0.85
            log_level: str = "INFO"
            max_heals_per_transaction: int = 2
            rolling_window_seconds: float = 60.0
            rolling_window_max_heals: int = 5
            circuit_cooldown_seconds: float = 30.0
            hmac_secret: str = "dev-secret-change-me-in-production"

        return cls(**_EnvLoader().model_dump())
