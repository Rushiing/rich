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

    # Claude API. ANTHROPIC_BASE_URL lets you point to a proxy/wrapper service
    # (e.g., the user's zenmux.ai endpoint). Empty = use the official endpoint.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""

    # Toggle the in-process APScheduler. Set False during local tests / when
    # running multiple replicas (only one should schedule).
    SCHEDULER_ENABLED: bool = True

    # Skip the password gate entirely. Intended ONLY for the testing window
    # before the tool gets handed to its real audience — anyone with the URL
    # gets full access while this is True. Default False so production stays
    # locked unless this is explicitly flipped on Railway.
    AUTH_DISABLED: bool = False


settings = Settings()
