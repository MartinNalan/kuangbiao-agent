from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.deepseek.com", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="deepseek-v4-flash", alias="OPENAI_MODEL")
    knowledge_base_url: str = Field(default="", alias="KNOWLEDGE_BASE_URL")
    enable_sync_web_supplement: bool = Field(default=False, alias="ENABLE_SYNC_WEB_SUPPLEMENT")
    api_keys: str = Field(default="", alias="API_KEYS")
    api_key_registry_path: str = Field(default="", alias="API_KEY_REGISTRY_PATH")
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="REDIS_URL")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_per_minute: int = Field(default=30, alias="RATE_LIMIT_PER_MINUTE")
    request_timeout_seconds: float = Field(default=60.0, alias="REQUEST_TIMEOUT_SECONDS")

    @property
    def chat_completions_url(self) -> str:
        return self.openai_base_url.rstrip("/") + "/chat/completions"

    @property
    def allowed_api_keys(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
