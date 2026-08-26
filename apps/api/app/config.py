from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Video Factory API"
    environment: str = "development"
    database_url: str = (
        "postgresql+psycopg://video_factory:video_factory_dev@localhost:5432/video_factory"
    )
    auth_jwks_url: str = "http://localhost:3000/api/auth/jwks"
    auth_issuer: str = "http://localhost:3000"
    auth_audience: str = "video-factory-api"
    storage_root: Path = Path("./data/storage")
    max_upload_bytes: int = 50 * 1024 * 1024
    storage_claim_secret: SecretStr = SecretStr(
        "development-only-storage-claim-secret"
    )
    storage_claim_ttl_seconds: Annotated[int, Field(gt=0, le=900)] = 300
    hatchet_client_token_file: Path | None = None
    hatchet_client_host_port: str = "localhost:7077"
    hatchet_server_url: str = "http://localhost:8888"
    outbox_dispatcher_enabled: bool = True
    outbox_poll_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 0.5
    mock_provider_webhook_secret: str | None = None
    provider_webhook_max_bytes: Annotated[int, Field(gt=0, le=1_048_576)] = 65_536
    ffprobe_binary: str = "ffprobe"
    ffmpeg_binary: str = "ffmpeg"
    media_probe_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10
    media_decode_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60
    media_cpu_time_seconds: Annotated[int, Field(gt=0, le=600)] = 60
    media_probe_max_memory_bytes: Annotated[
        int, Field(ge=67_108_864, le=4_294_967_296)
    ] = 1_073_741_824
    media_decode_max_memory_bytes: Annotated[
        int, Field(ge=67_108_864, le=4_294_967_296)
    ] = 2_147_483_648
    media_max_output_bytes: Annotated[int, Field(gt=0, le=4_194_304)] = 1_048_576
    media_cpu_count: Annotated[int, Field(gt=0, le=4)] = 1
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def reject_development_claim_secret_in_production(self) -> "Settings":
        secret = self.storage_claim_secret.get_secret_value()
        if len(secret.encode()) < 32:
            raise ValueError("STORAGE_CLAIM_SECRET must contain at least 32 bytes")
        if self.environment == "production" and secret.startswith("development-only-"):
            raise ValueError("STORAGE_CLAIM_SECRET must be replaced in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
