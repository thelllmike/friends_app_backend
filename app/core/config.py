"""Application settings loaded from environment / .env."""
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # General
    app_name: str = "Friends API"
    environment: str = "development"  # development | production
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    cors_origins: CsvList = Field(default_factory=lambda: ["*"])

    # Storage
    database_url: str = "postgresql+asyncpg://friends:friends@localhost:5432/friends"
    redis_url: str = "redis://localhost:6379/0"
    media_dir: str = "./media"
    media_base_url: str = "http://localhost:8000/media"
    max_photo_bytes: int = 5 * 1024 * 1024

    # JWT
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    admin_token_minutes: int = 60 * 8

    # Social login audiences (client IDs / bundle IDs that may appear in `aud`)
    google_client_ids: CsvList = Field(default_factory=list)
    apple_audiences: CsvList = Field(default_factory=list)

    # OTP
    otp_provider: str = "console"  # console | <your sms provider>
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_dev_echo: bool = False  # return the code in the API response (dev only!)

    # TURN (coturn use-auth-secret)
    turn_secret: str = "change-me-turn"
    turn_host: str = "turn.example.com"
    turn_ttl_seconds: int = 3600
    turn_realm: str = "friends"
    # turns: ports advertised to clients. 443 only works if coturn owns 443 on its own IP (see README).
    turn_tls_ports: CsvList = Field(default_factory=lambda: ["5349"])

    # Matchmaking / WebSocket
    search_timeout_seconds: int = 30
    ws_idle_timeout_seconds: int = 60
    ws_max_messages_per_second: int = 30
    chat_max_length: int = 500
    chat_max_per_10s: int = 10
    queue_scan_limit: int = 200

    # Billing
    billing_fake_mode: bool = False  # accept "test_*" tokens without contacting stores (dev only!)
    google_play_package_name: str = ""
    google_service_account_file: str = ""
    apple_bundle_id: str = ""
    apple_issuer_id: str = ""
    apple_key_id: str = ""
    apple_private_key_file: str = ""

    # Rate limits
    login_rate_per_minute: int = 10
    otp_requests_per_10min: int = 3
    reports_per_hour: int = 10

    @field_validator("cors_origins", "google_client_ids", "apple_audiences", "turn_tls_ports", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # Allow comma-separated env values in addition to JSON lists.
        if isinstance(v, str) and not v.strip().startswith("["):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def check_production(self) -> None:
        """Refuse to boot production with dev secrets."""
        if not self.is_production:
            return
        problems = []
        if len(self.jwt_secret) < 32 or "change" in self.jwt_secret:
            problems.append("JWT_SECRET must be a random string of 32+ chars")
        if len(self.turn_secret) < 24 or "change" in self.turn_secret:
            problems.append("TURN_SECRET must be a random string of 24+ chars")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must list explicit origins in production")
        if problems:
            raise RuntimeError("Unsafe production config: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
