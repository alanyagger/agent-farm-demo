from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "智能体凭证农场 API"
    database_url: str = "sqlite:///./data/agent_farm.db"
    scheduler_interval_seconds: float = 2.0
    credential_provider: str = "mock"
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # The default keeps the offline demo usable. Set this to "llm" to make
    # every agent turn use the configured external model provider.
    agent_runtime_mode: str = "rules"
    model_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout_seconds: float = 20.0
    agent_max_tool_rounds: int = 4

    cmcc_base_url: str = ""
    cmcc_app_id: str = ""
    cmcc_app_key: str = ""
    cmcc_client_secret: str = ""
    cmcc_agent_template_id: str = ""
    cmcc_demo_phone: str = ""
    cmcc_admission_mode: str = "off"
    cmcc_admission_agent_mappings: str = ""
    # Used as the primary mapping by older .env files.
    cmcc_admission_agent_id: str = "agent-sprout"
    cmcc_admission_agent_name: str = "agent1"
    cmcc_admission_timeout_seconds: float = 3.0
    cmcc_admission_cache_seconds: float = 10.0

    openclaw_farm_enabled: bool = False
    openclaw_farm_token: str = ""
    openclaw_farm_run_ttl_seconds: int = 300
    openclaw_model_label: str = "deepseek/deepseek-v4-pro"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def llm_runtime_enabled(self) -> bool:
        return self.agent_runtime_mode.lower() == "llm"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
