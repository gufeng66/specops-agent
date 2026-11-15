"""环境配置。CI / Compose 默认 LLM_PROVIDER=none，保证无 Key 也能演示和评测。"""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CHECKPOINT_MODES = ("sqlite", "memory")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: str = "none"  # CI 默认；本地演示改为 deepseek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2  # 不含首次；仅 429/5xx/传输错误。超时不重试
    llm_retry_base_seconds: float = 0.4  # 测试置 0，避免真睡
    llm_retry_budget_seconds: float = 8  # 单次 post_completions 退避睡眠总和上限
    llm_max_concurrency: int = 3
    llm_max_calls_per_run: int = 128  # 一次 /chat/stream 或 /resume，不含人工停留
    cors_origins: str = "http://localhost:5173"
    app_env: str = "development"  # development / test / production
    checkpoint_mode: Literal["sqlite", "memory"] = "sqlite"  # sqlite（持久化）| memory（仅测试/演示）

    @field_validator("checkpoint_mode")
    @classmethod
    def _check_checkpoint_mode(cls, value: str) -> str:
        """拒绝拼写错误的 CHECKPOINT_MODE，避免被当成 sqlite 静默运行。"""
        if value not in CHECKPOINT_MODES:
            raise ValueError(
                f"CHECKPOINT_MODE 必须是 {' 或 '.join(CHECKPOINT_MODES)}，收到 {value!r}；"
                "不会静默回退到 sqlite"
            )
        return value
    database_path: str = "./specops.db"
    checkpoint_path: str = "./specops_checkpoints.db"
    request_retention_days: int = 7  # processed_requests 快照保留窗口
    max_tool_rounds: int = 4  # validate_repair 工具循环上限
    max_consecutive_tool_failures: int = 2
    max_tool_stall: int = 2  # error 数不下降则停，防止空转
    llm_chunk_chars: int = 3500
    llm_chunk_overlap: int = 200
    llm_brief_max_chars: int = 12000
    source_soft_limit_chars: int = 8000  # 提示将分块，不拒绝
    source_hard_limit_chars: int = 100000  # 超过直接 422

    @property
    def cors_origin_list(self) -> list[str]:
        """逗号分隔的 CORS 来源。Compose / 本地 Vite 默认 5173。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
