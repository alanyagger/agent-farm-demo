from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "智能体凭证农场 API"
    database_url: str = "sqlite:///./data/agent_farm.db"
    scheduler_interval_seconds: float = 2.0
    credential_provider: str = "mock"
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    cmcc_base_url: str = ""
    cmcc_app_id: str = ""
    cmcc_app_key: str = ""
    cmcc_client_secret: str = ""
    cmcc_agent_template_id: str = ""
    cmcc_demo_phone: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
