"""Application settings: secrets from environment (.env), tunables from config.yaml."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelRef(BaseModel):
    provider: str
    model: str


class EmbeddingRef(ModelRef):
    dimensions: int = 768


class LLMConfig(BaseModel):
    chat: list[ModelRef] = Field(default_factory=list)
    internal: list[ModelRef] = Field(default_factory=list)
    vision: list[ModelRef] = Field(default_factory=list)
    embedding: EmbeddingRef | None = None
    max_output_tokens: int = 400
    timeout_seconds: float = 20.0  # per chat request; then fall back to the next provider
    pricing: dict[str, Any] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str | None = None


class RetrievalConfig(BaseModel):
    top_k: int = 4
    candidates: int = 20
    rrf_k: int = 60


class ThresholdConfig(BaseModel):
    direct_return_cosine: float = 0.90
    retrieval_min_cosine: float = 0.60
    llm_confidence: float = 0.70
    semantic_cache: float = 0.95


class CacheConfig(BaseModel):
    ttl_hours: int = 168


class QuotaConfig(BaseModel):
    daily_requests: int = 200
    daily_tokens: int = 100_000
    timezone: str = "Asia/Bangkok"


class UploadConfig(BaseModel):
    max_file_mb: int = 20
    multipart_max_mb: int = 4


class ConversionConfig(BaseModel):
    ocr_enabled: bool = True
    min_ocr_chars: int = 20
    job_timeout_minutes: int = 5
    max_attempts: int = 3


class ConversationConfig(BaseModel):
    history_messages: int = 6


class CorsConfig(BaseModel):
    allow_origins: list[str] = Field(default_factory=lambda: ["*"])


class AppConfig(BaseModel):
    """Non-secret tunables loaded from config.yaml."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    conversion: ConversionConfig = Field(default_factory=ConversionConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    retention_days: int = 90
    cors: CorsConfig = Field(default_factory=CorsConfig)


class Settings(BaseSettings):
    """Secrets and environment-specific values."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"
    config_path: Path = Path("config.yaml")

    database_url: str | None = None
    database_url_direct: str | None = None
    database_url_llm_readonly: str | None = None

    supabase_url: str | None = None
    supabase_service_key: str | None = None

    admin_bootstrap_key: str | None = None
    cron_secret: str | None = None

    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    jina_api_key: str | None = None


def load_app_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_app_config() -> AppConfig:
    return load_app_config(get_settings().config_path)
