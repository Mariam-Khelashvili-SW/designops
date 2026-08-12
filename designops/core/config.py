"""Env-driven settings (pydantic-settings). Secrets come from env ONLY —
never the DB, never git (§10)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://designops:designops@localhost:5435/designops",
        alias="DATABASE_URL",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_scheme(cls, v):
        # Managed Postgres (Railway/Render/Fly/Heroku) hands out a plain `postgresql://`
        # or legacy `postgres://` URL. SQLAlchemy maps those to psycopg2, which we don't
        # ship — pin the psycopg-v3 driver so the platform's URL works as-is.
        if isinstance(v, str):
            if v.startswith("postgres://"):
                v = "postgresql+psycopg://" + v[len("postgres://"):]
            elif v.startswith("postgresql://"):
                v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    # Fairwind REST (OAuth2 client-credentials, §10)
    fw_base_url: str = Field(default="https://fairwind.scandiweb.com", alias="FW_BASE_URL")
    fw_client_id: str = Field(default="", alias="FW_CLIENT_ID")
    fw_client_secret: str = Field(default="", alias="FW_CLIENT_SECRET")
    fw_resource: str = Field(
        default="https://fairwind.scandiweb.com/api/v1", alias="FW_RESOURCE"
    )
    fw_scope: str = Field(default="api", alias="FW_SCOPE")
    # Export data_types pulled from Fairwind for the daily report. The team's daily
    # reports live in the internal email threads, so the digest searches emails_internal
    # (plus jira for the cross-check, external + transcripts for context). Overridable
    # via FW_DATA_TYPES="emails_internal,jira".
    # NoDecode: hand the raw env string to _split_data_types instead of letting
    # pydantic-settings JSON-decode it (a plain "a,b,c" value isn't valid JSON and
    # would raise a SettingsError at boot).
    fw_data_types: Annotated[list[str], NoDecode] = Field(
        default=["emails_internal", "emails_external", "jira", "transcripts"],
        alias="FW_DATA_TYPES",
    )

    @field_validator("fw_data_types", mode="before")
    @classmethod
    def _split_data_types(cls, v):
        # accept a comma-separated env string as well as a real list
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    # Parallel Fairwind exports (create→poll→download per account). Higher = faster
    # digests when many mentioned accounts; keep modest to avoid 409 storms.
    fw_export_concurrency: int = Field(default=8, alias="FW_EXPORT_CONCURRENCY")
    fw_export_poll_interval_s: float = Field(default=2.0, alias="FW_EXPORT_POLL_INTERVAL_S")
    # TEMPORARY: daily uses CRO + direct Jira only (no Fairwind mention exports).
    # Set DAILY_SKIP_FAIRWIND=0 to re-enable Pass B Fairwind pulls.
    daily_skip_fairwind: bool = Field(default=True, alias="DAILY_SKIP_FAIRWIND")

    # Anthropic
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    digest_model: str = Field(default="claude-opus-4-8", alias="DIGEST_MODEL")
    # Cheaper/faster tier for call-summary critic pass (v2).
    call_summary_critic_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="CALL_SUMMARY_CRITIC_MODEL"
    )

    # Jira Cloud (direct REST — A3 weekly backlog; email + API token Basic auth)
    jira_base_url: str = Field(default="", alias="JIRA_BASE_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    normal_week_hours: float = Field(default=40.0, alias="NORMAL_WEEK_HOURS")

    # Tempo Cloud (VACSICK leave detection — Bearer token)
    tempo_api_token: str = Field(default="", alias="TEMPO_API_TOKEN")
    tempo_api_base: str = Field(default="https://api.tempo.io/4", alias="TEMPO_API_BASE")

    # Pipeline behaviour
    timezone: str = Field(default="Europe/Riga", alias="TIMEZONE")
    min_coverage: float = Field(default=0.6, alias="MIN_COVERAGE")
    corpus_store_dir: str = Field(default="./var/corpus", alias="CORPUS_STORE_DIR")

    # Delivery — Google OAuth (preferred: click-to-authorize, revocable) …
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    # Explicit override. If unset, derived from PUBLIC_APP_URL / RAILWAY_PUBLIC_DOMAIN.
    google_redirect_uri: str = Field(default="", alias="GOOGLE_REDIRECT_URI")
    public_app_url: str = Field(default="", alias="PUBLIC_APP_URL")
    google_token_path: str = Field(
        default="./var/google_oauth.json", alias="GOOGLE_TOKEN_PATH"
    )

    @field_validator("google_redirect_uri", mode="before")
    @classmethod
    def _strip_redirect_uri(cls, v):
        if isinstance(v, str) and v.strip():
            return v.strip().rstrip("/")
        return v

    def model_post_init(self, __context) -> None:
        import os

        base = (self.public_app_url or "").strip().rstrip("/")
        if not base:
            host = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
            if host:
                base = host if host.startswith("http") else f"https://{host}"
        # PUBLIC_APP_URL / RAILWAY_PUBLIC_DOMAIN → default Google OAuth callback when unset.
        if not self.google_redirect_uri:
            object.__setattr__(
                self,
                "google_redirect_uri",
                (
                    f"{base}/oauth/google/callback"
                    if base
                    else "http://localhost:8077/oauth/google/callback"
                ),
            )
    # … and SMTP (Gmail app password) as the simpler fallback sender
    gmail_sender: str = Field(default="", alias="GMAIL_SENDER")  # the From / SMTP login
    gmail_app_password: str = Field(default="", alias="GMAIL_APP_PASSWORD")
    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    olga_email: str = Field(default="", alias="OLGA_EMAIL")
    setup_owner_email: str = Field(
        default="liana.staskevica@scandiweb.com", alias="SETUP_OWNER_EMAIL"
    )
    # CRO shared mailbox — Gmail read only (alias on a connected Google account)
    cro_mailbox_email: str = Field(
        default="cro@scandiweb.com", alias="CRO_MAILBOX_EMAIL"
    )

    # Transcript app — calendar meetings for weekly health last/next call dates
    transcript_api_base_url: str = Field(
        default="http://localhost:3001", alias="TRANSCRIPT_API_BASE_URL"
    )
    transcript_api_token: str = Field(default="", alias="TRANSCRIPT_API_TOKEN")

    # Notion — design intake page publishing
    notion_api_token: str = Field(default="", alias="NOTION_API_TOKEN")
    # Parent page ID (dev/sample) or database ID (production Design projects DB)
    notion_parent_page_id: str = Field(default="", alias="NOTION_PARENT_PAGE_ID")
    # When true, NOTION_PARENT_PAGE_ID is treated as a database_id
    notion_parent_is_database: bool = Field(default=False, alias="NOTION_PARENT_IS_DATABASE")

    @property
    def fairwind_configured(self) -> bool:
        return bool(self.fw_client_id and self.fw_client_secret)

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def jira_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    @property
    def tempo_configured(self) -> bool:
        return bool(self.tempo_api_token)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.gmail_sender and self.gmail_app_password)

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def transcript_api_configured(self) -> bool:
        return bool(self.transcript_api_base_url and self.transcript_api_token)

    @property
    def notion_configured(self) -> bool:
        return bool(self.notion_api_token and self.notion_parent_page_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
