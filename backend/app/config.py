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
    ANTHROPIC_API_KEY: str = ""


settings = Settings()
