from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MIM Core"
    app_version: str = "0.1.0"
    environment: str = "local"

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/mim",
        alias="DATABASE_URL",
    )

    journal_name: str = "mim_journal"
    allow_openai: bool = False
    allow_web_access: bool = False
    allow_local_devices: bool = True


settings = Settings()
