from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_PASSWORD: str = "change-me"
    AUTH_SECRET: str = "change-me-to-32-bytes-of-random-hex"
    DATABASE_URL: str = "postgresql+psycopg://rich:rich@localhost:5432/rich"
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # Claude / analysis-LLM API. ANTHROPIC_BASE_URL lets you point at any
    # Anthropic-compatible gateway — zenmux, dashscope coding plan, etc.
    # Empty = official api.anthropic.com.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    # Which model name to send to the gateway. Empty = use DEFAULT_MODEL
    # baked into services/analysis.py (currently kimi-k2.5 via dashscope).
    # Override per-deployment if the gateway shifts available models or you
    # want to A/B test (e.g., "claude-sonnet-4-6" on zenmux).
    ANALYSIS_MODEL: str = ""

    # Toggle the in-process APScheduler. Set False during local tests / when
    # running multiple replicas (only one should schedule).
    SCHEDULER_ENABLED: bool = True

    # Skip the password gate entirely. Intended ONLY for the testing window
    # before the tool gets handed to its real audience — anyone with the URL
    # gets full access while this is True. Default False so production stays
    # locked unless this is explicitly flipped on Railway.
    AUTH_DISABLED: bool = False


settings = Settings()
