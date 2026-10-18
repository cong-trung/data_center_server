from typing import Annotated, Literal

from pydantic import PositiveInt, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PolarsEngine = Literal["auto", "in-memory", "streaming", "gpu"]


class Settings(BaseSettings):
    """Application settings, read from environment variables and the `.env` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "Data Center Server"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    API_V1_STR: str = "/api/v1"
    # Comma-separated origins allowed to call the API from a browser.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = []

    MAX_UPLOAD_SIZE_MB: PositiveInt = 200
    # Files processed at the same time; bounds RAM/VRAM usage under load.
    MAX_CONCURRENT_JOBS: PositiveInt = 2
    POLARS_ENGINE: PolarsEngine = "auto"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def max_json_body_size_bytes(self) -> int:
        """JSON requests carry the file base64-encoded (4 bytes per 3), plus 1 MB for the rest."""
        return 4 * ((self.max_upload_size_bytes + 2) // 3) + 1024 * 1024


settings = Settings()
