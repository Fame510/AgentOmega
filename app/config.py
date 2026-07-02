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

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
