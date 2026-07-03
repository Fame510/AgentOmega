from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Browser
    HEADLESS: bool = Field(True, env="HEADLESS")
    VIEWPORT_WIDTH: int = Field(1280, env="VIEWPORT_WIDTH")
    VIEWPORT_HEIGHT: int = Field(720, env="VIEWPORT_HEIGHT")
    DEFAULT_TIMEOUT_MS: int = Field(5000, env="DEFAULT_TIMEOUT_MS")
    USER_AGENT: str = Field(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        env="USER_AGENT"
    )

    # LLM / VLM endpoints
    PLANNER_LLM_URL: str = Field("http://localhost:11434/api/generate", env="PLANNER_LLM_URL")
    PLANNER_MODEL: str = Field("llama3.2", env="PLANNER_MODEL")
    VLM_URL: str = Field("http://localhost:11434/api/generate", env="VLM_URL")
    VLM_MODEL: str = Field("llava", env="VLM_MODEL")

    # Execution
    MAX_STEPS: int = Field(20, env="MAX_STEPS")
    MAX_RETRIES_PER_ACTION: int = Field(3, env="MAX_RETRIES_PER_ACTION")

    # Redis (optional)
    REDIS_URL: str = Field("redis://localhost:6379", env="REDIS_URL")
    USE_REDIS_SESSION: bool = Field(False, env="USE_REDIS_SESSION")

    # Firecrawl (optional)
    FIRECRAWL_API_KEY: str = Field("", env="FIRECRAWL_API_KEY")
    FIRECRAWL_API_URL: str = Field("https://api.firecrawl.dev/v1", env="FIRECRAWL_API_URL")

    # -----------------------------------------------------------------
    # SHACKLE governor policy
    # -----------------------------------------------------------------
    SHACKLE_BUDGET_USD: float = Field(1.0, env="SHACKLE_BUDGET_USD")
    SHACKLE_MAX_REPEAT_CALLS: int = Field(3, env="SHACKLE_MAX_REPEAT_CALLS")
    SHACKLE_ERROR_AMPLIFICATION: bool = Field(True, env="SHACKLE_ERROR_AMPLIFICATION")
    SHACKLE_MAX_TOTAL_CALLS: int = Field(200, env="SHACKLE_MAX_TOTAL_CALLS")
    # hitl_mode: never | on_deny | on_threshold | always
    SHACKLE_HITL_MODE: str = Field("on_threshold", env="SHACKLE_HITL_MODE")
    SHACKLE_HITL_BUDGET_THRESHOLD: float = Field(0.15, env="SHACKLE_HITL_BUDGET_THRESHOLD")
    # Bounded HITL wait: a disconnected operator can NEVER hang the worker.
    SHACKLE_HITL_TIMEOUT_S: int = Field(120, env="SHACKLE_HITL_TIMEOUT_S")

    # Audit ledger
    SHACKLE_AUDIT_PATH: str = Field("./data/audit.jsonl", env="SHACKLE_AUDIT_PATH")
    SHACKLE_SIGNING_KEY_HEX: str = Field("", env="SHACKLE_SIGNING_KEY_HEX")

    # Governance API auth (bearer token). Empty => API fails closed (503).
    SHACKLE_API_TOKEN: str = Field("", env="SHACKLE_API_TOKEN")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
