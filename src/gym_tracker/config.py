from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
