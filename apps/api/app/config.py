from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "Video Factory API"
    environment: str = "development"
    workflow_backend: Literal["local", "hatchet"] = "local"
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
    hatchet_client_token: SecretStr = Field(default=SecretStr(""), repr=False)
    hatchet_client_token_file: Path | None = None
    # Omitted addresses come from the Hatchet token; local defaults must not mask Cloud claims.
    hatchet_client_host_port: str | None = None
    hatchet_server_url: str | None = None
    hatchet_client_namespace: str = ""
    hatchet_client_tls_strategy: Literal["tls", "none"] = "tls"
    hatchet_client_tls_server_name: str = ""
    hatchet_client_tls_root_ca_file: Path | None = None
    outbox_dispatcher_enabled: bool = True
    outbox_poll_interval_seconds: Annotated[float, Field(gt=0, le=60)] = 0.5
    outbox_max_attempts: Annotated[int, Field(gt=0, le=100)] = 5
    reconciler_enabled: bool = True
    reconciler_interval_seconds: Annotated[float, Field(gt=0, le=300)] = 30
    reconciler_stuck_after_seconds: Annotated[int, Field(gt=0, le=86_400)] = 300
    mock_provider_webhook_secret: str | None = None
    generation_route_version: Literal[
        "mock_video_v1", "runpod_simulated_v1"
    ] = "mock_video_v1"
    generation_route_enabled: bool = True
    runpod_simulator_enabled: bool = True
    runpod_provider_enabled: bool = False
    provider_callback_claim_secret: SecretStr = SecretStr(
        "development-only-provider-callback-claim-secret"
    )
    provider_claim_ttl_seconds: Annotated[int, Field(gt=0, le=900)] = 300
    provider_webhook_max_bytes: Annotated[int, Field(gt=0, le=1_048_576)] = 65_536
    admin_auth_subjects: Annotated[list[str], NoDecode] = []
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @model_validator(mode="after")
    def validate_workflow_backend(self) -> "Settings":
        if self.environment == "production" and self.workflow_backend == "local":
            raise ValueError(
                "WORKFLOW_BACKEND=local is development-only; use hatchet in production"
            )
        if self.workflow_backend == "hatchet":
            self.validate_hatchet_addresses(
                self.hatchet_client_host_port, self.hatchet_server_url
            )
            if self.hatchet_client_tls_root_ca_file is not None:
                if not self.hatchet_client_tls_root_ca_file.is_file():
                    raise ValueError("HATCHET_CLIENT_TLS_ROOT_CA_FILE is unavailable")
            self.get_hatchet_token()
        return self

    def validate_hatchet_addresses(self, host_port: str | None, server_url: str | None) -> None:
        """Validate explicit settings and resolved token addresses before sending credentials."""
        local_hosts = {"localhost", "127.0.0.1", "::1", "hatchet"}
        if self.hatchet_client_tls_strategy == "none":
            if self.environment == "production":
                raise ValueError("Production Hatchet requires HATCHET_CLIENT_TLS_STRATEGY=tls")
            if self.hatchet_client_tls_root_ca_file or self.hatchet_client_tls_server_name:
                raise ValueError("Hatchet TLS options cannot be used with strategy=none")
        if host_port is not None:
            try:
                host = urlsplit(f"//{host_port}")
                valid = (
                    bool(host.hostname) and host.port is not None and 1 <= host.port <= 65535
                    and host.username is None and host.password is None
                    and not host.path and not host.query and not host.fragment
                    and not any(character.isspace() for character in host_port)
                    and "\\" not in host_port
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("HATCHET_CLIENT_HOST_PORT must be host:port, without a URL scheme")
            if self.hatchet_client_tls_strategy == "none" and host.hostname not in local_hosts:
                raise ValueError("Remote Hatchet gRPC requires TLS; none is local-only")
        if server_url is not None:
            try:
                server = urlsplit(server_url)
                valid = (
                    server.scheme in {"http", "https"} and bool(server.hostname)
                    and server.username is None and server.password is None
                    and server.path in {"", "/"} and not server.query and not server.fragment
                    and not any(character.isspace() for character in server_url)
                    and "\\" not in server_url
                    and (server.port is None or 1 <= server.port <= 65535)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("HATCHET_SERVER_URL must be an HTTP(S) origin without credentials")
            if self.hatchet_client_tls_strategy == "tls" and server.scheme != "https":
                raise ValueError("Hatchet TLS requires an HTTPS HATCHET_SERVER_URL")
            if self.hatchet_client_tls_strategy == "none" and server.hostname not in local_hosts:
                raise ValueError("Remote Hatchet REST requires TLS; none is local-only")

    def get_hatchet_token(self) -> str:
        token = self.hatchet_client_token.get_secret_value().strip()
        if self.hatchet_client_token_file is not None:
            try:
                token = self.hatchet_client_token_file.read_text(encoding="utf-8").strip()
            except OSError:
                raise ValueError("HATCHET_CLIENT_TOKEN_FILE is unavailable") from None
        if not token:
            raise ValueError("HATCHET_CLIENT_TOKEN or HATCHET_CLIENT_TOKEN_FILE must be configured")
        return token

    @field_validator("admin_auth_subjects", "cors_origins", mode="before")
    @classmethod
    def parse_comma_separated_values(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def reject_development_claim_secret_in_production(self) -> "Settings":
        secret = self.storage_claim_secret.get_secret_value()
        callback_secret = self.provider_callback_claim_secret.get_secret_value()
        if len(secret.encode()) < 32:
            raise ValueError("STORAGE_CLAIM_SECRET must contain at least 32 bytes")
        if len(callback_secret.encode()) < 32:
            raise ValueError(
                "PROVIDER_CALLBACK_CLAIM_SECRET must contain at least 32 bytes"
            )
        if self.environment == "production" and secret.startswith("development-only-"):
            raise ValueError("STORAGE_CLAIM_SECRET must be replaced in production")
        if self.environment == "production" and callback_secret.startswith(
            "development-only-"
        ):
            raise ValueError(
                "PROVIDER_CALLBACK_CLAIM_SECRET must be replaced in production"
            )
        if self.runpod_provider_enabled:
            raise ValueError(
                "RUNPOD_PROVIDER_ENABLED cannot be enabled before the real adapter is installed"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
