from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://gym_tracker:gym_tracker@localhost:5432/gym_tracker"
    data_backend: Literal["postgres", "csv"] = "postgres"
    legacy_csv_path: Path = Path("academia_treinos_whatsapp.csv")
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-5.4-mini"
    llm_shadow_mode: bool = True
    llm_auto_accept: bool = False
    parser_version: str = "3.0.0"
    gym_tracker_user_id: UUID | None = None
    gym_import_file: Path = Path("whatsapp_acad_export/Conversa do WhatsApp com Acad.txt")
    gym_import_interval_seconds: int = Field(default=60, ge=10, le=86400)
    gym_import_settle_seconds: int = Field(default=10, ge=1, le=300)
    gym_backup_container: str | None = None
    gym_backup_directory: Path = Path("local_backups/automatic")

    @field_validator("gym_tracker_user_id", mode="before")
    @classmethod
    def empty_user_is_unconfigured(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
