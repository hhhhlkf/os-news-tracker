from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///:memory:"
    llm_base_url: str = "http://llm.invalid/v1"
    llm_api_key: str = "test-key"
    llm_model: str = "test-model"
    llm_max_concurrency: int = 4
    search_provider: str = "none"
    fetch_user_agent: str = "os-news-tracker/0.1 (+internal)"
    fetch_per_host_delay_seconds: float = 2.0
    missing_date_policy: str = "include_as_now"
    manual_fetch_max_workers: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
