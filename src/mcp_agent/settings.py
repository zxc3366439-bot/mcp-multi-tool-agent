"""Central environment configuration; secrets never appear in repr output."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    llm_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    mcp_config_path: Path | None = None
    mcp_startup_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    agent_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    agent_recursion_limit: int = Field(default=20, ge=2, le=200)

    def require_llm(self) -> None:
        if not self.llm_model.strip() or self.llm_model == "your-tool-calling-model":
            raise ValueError("请在 .env 中填写 LLM_MODEL（须支持 tool calling）。")
        if not self.llm_api_key or not self.llm_api_key.get_secret_value().strip():
            raise ValueError("请在 .env 中填写 LLM_API_KEY。")
        if not self.llm_base_url.startswith(("https://", "http://")):
            raise ValueError("LLM_BASE_URL 必须是 http:// 或 https:// API 地址。")
