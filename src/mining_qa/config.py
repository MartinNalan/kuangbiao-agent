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
    request_timeout_seconds: float = Field(default=60.0, alias="REQUEST_TIMEOUT_SECONDS")

    @property
    def chat_completions_url(self) -> str:
        return self.openai_base_url.rstrip("/") + "/chat/completions"


@lru_cache
def get_settings() -> Settings:
    return Settings()
