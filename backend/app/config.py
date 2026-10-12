from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Developer AI Assistant"
    database_url: str = "sqlite:///./data/assistant.db"
    default_model: str = Field(
        default="deepseek-flash",
        validation_alias=AliasChoices("DEFAULT_MODEL", "AI_HELPER_DEFAULT_MODEL"),
    )
    default_context_window: int = Field(default=16_384, ge=4_096, le=131_072)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=0)
    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "AI_HELPER_DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "AI_HELPER_DEEPSEEK_BASE_URL"),
    )
    target_repo_path: str = Field(
        default="/repos/aurelSpa",
        validation_alias=AliasChoices("TARGET_REPO_PATH", "AI_HELPER_TARGET_REPO_PATH"),
    )
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    rag_top_k: int = Field(default=8, ge=1, le=30)
    rag_min_score: float = Field(default=0.08, ge=0, le=1)
    rag_token_budget: int = Field(default=5_000, ge=256, le=32_000)
    max_output_tokens: int = Field(default=2_048, ge=256, le=16_384)
    auto_index: bool = True

    model_config = SettingsConfigDict(
        env_prefix="AI_HELPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
