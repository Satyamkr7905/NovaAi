# API settings loaded from .env. Separate from app.config which is for the CLI.

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_log = logging.getLogger(__name__)

# strings we refuse to accept as JWT secret — anything here means "unset".
# keeps someone from copying .env.example verbatim and shipping it.
_INSECURE_JWT_DEFAULTS: frozenset[str] = frozenset(
    {
        "",
        "change-me-in-production-use-openssl-rand-hex-32",
        "generate-a-long-random-string",
        "your-secret",
        "secret",
        "dev",
    }
)


def _default_postgres_url() -> str:
    # local dev default; override with DATABASE_URL in .env.
    return "postgresql+psycopg://nova:nova@127.0.0.1:5432/dsa_by_nova"


class ApiSettings(BaseSettings):
    # loads from adaptive_dsa_agent/.env.

    # `dev` is loose (allows weak JWT secret + returns OTP in response when SMTP off).
    # `prod` is strict — won't start with an insecure secret.
    app_env: str = "dev"

    # SQLAlchemy URL. postgres needs `postgresql+psycopg://` (psycopg3).
    # sqlite works too: `sqlite:///./data/tutor_api.db`.
    database_url: str = Field(default_factory=_default_postgres_url)
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Google Sign-In web client ID from Google Cloud Console.
    google_client_id: str = ""

    # Gmail SMTP for OTP mail. use an App Password (not your normal login).
    # port 465 works when STARTTLS on 587 is blocked by your network.
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 587
    gmail_smtp_timeout_seconds: int = 20
    gmail_user: str = ""
    gmail_app_password: str = ""
    # Optional transactional email provider for production (recommended on Render).
    resend_api_key: str = ""
    resend_from_email: str = ""

    otp_ttl_minutes: int = 10

    # after this many wrong tries the OTP is killed — user has to request new one.
    otp_max_attempts: int = 5

    # per-email rate limit on /send-otp. best-effort, in-process only.
    # behind a proxy you want IP-level limits too.
    otp_send_cooldown_seconds: int = 30
    otp_send_daily_max: int = 20
    # Emergency switch: allow returning OTP inline in production when email
    # delivery fails. Keep false for normal operation.
    otp_allow_inline_fallback_in_prod: bool = False

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    model_config = SettingsConfigDict(
        env_file=str(_AGENT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("app_env")
    @classmethod
    def _norm_env(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return v if v in {"dev", "prod", "test"} else "dev"

    def is_prod(self) -> bool:
        return self.app_env == "prod"

    def jwt_secret_is_insecure(self) -> bool:
        s = (self.jwt_secret or "").strip()
        return s in _INSECURE_JWT_DEFAULTS or len(s) < 32


@lru_cache
def get_api_settings() -> ApiSettings:
    s = ApiSettings()
    if s.jwt_secret_is_insecure():
        msg = (
            "JWT_SECRET is unset or insecure (need a random string of at least "
            "32 chars — e.g. `python -c \"import secrets;print(secrets.token_hex(32))\"`)."
        )
        if s.is_prod():
            # fail closed in prod — a short/known secret lets anyone forge tokens.
            raise RuntimeError(msg)
        _log.warning("%s Running in APP_ENV=dev so we'll continue, but do NOT deploy this.", msg)
    return s


def cors_list() -> list[str]:
    # parse CORS_ORIGINS and strip any '*' — '*' + credentials is unsafe so we
    # refuse it instead of letting the browser complain later.
    raw = get_api_settings().cors_origins
    items = [o.strip() for o in raw.split(",") if o.strip()]
    safe = [o for o in items if o != "*"]
    if len(safe) != len(items):
        _log.warning(
            "CORS_ORIGINS contained '*' — ignored. Credentialed CORS needs an explicit origin list."
        )
    return safe
