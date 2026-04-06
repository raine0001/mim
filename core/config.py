from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Support both legacy root .env and workspace env/.env.
        # Later files override earlier ones when keys overlap.
        env_file=(
            str(PROJECT_ROOT / ".env"),
            str(PROJECT_ROOT / "env" / ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MIM Core"
    app_version: str = "0.1.0"
    environment: str = "local"
    release_tag: str = "dev"
    config_profile: str = "default"
    build_git_sha: str = "unknown"
    build_timestamp: str = "unknown"

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/mim",
        alias="DATABASE_URL",
    )

    journal_name: str = "mim_journal"
    allow_openai: bool = False
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    allow_web_access: bool = False
    allow_local_devices: bool = True
    vision_policy_path: str = str(PROJECT_ROOT / "config" / "vision_policy.json")
    voice_policy_path: str = str(PROJECT_ROOT / "config" / "voice_policy.json")
    execution_feedback_api_key: str = ""
    execution_feedback_allowed_actors: str = "tod,executor"
    execution_readiness_task_result_path: str = str(
        PROJECT_ROOT / "runtime" / "shared" / "TOD_MIM_TASK_RESULT.latest.json"
    )
    execution_readiness_command_status_path: str = str(
        PROJECT_ROOT / "runtime" / "shared" / "TOD_MIM_COMMAND_STATUS.latest.json"
    )

    # Automation/browser controls
    automation_enabled: bool = True
    automation_default_simulation: bool = True
    automation_allow_live_browser: bool = False
    automation_browser_headless: bool = True
    automation_storage_dir: str = str(PROJECT_ROOT / "runtime" / "automation")
    automation_default_timeout_seconds: int = 20
    automation_default_timezone: str = Field(
        default="America/Los_Angeles", alias="MIM_DEFAULT_TIMEZONE"
    )
    web_research_total_budget_seconds: int = Field(
        default=10, alias="WEB_RESEARCH_TOTAL_BUDGET_SECONDS"
    )
    web_research_search_timeout_seconds: int = Field(
        default=5, alias="WEB_RESEARCH_SEARCH_TIMEOUT_SECONDS"
    )
    web_research_fetch_timeout_seconds: int = Field(
        default=4, alias="WEB_RESEARCH_FETCH_TIMEOUT_SECONDS"
    )
    web_research_fetch_max_parallelism: int = Field(
        default=3, alias="WEB_RESEARCH_FETCH_MAX_PARALLELISM"
    )
    web_research_technical_default_budget_minutes: int = Field(
        default=15, alias="WEB_RESEARCH_TECHNICAL_DEFAULT_BUDGET_MINUTES"
    )
    web_research_technical_max_plan_steps: int = Field(
        default=4, alias="WEB_RESEARCH_TECHNICAL_MAX_PLAN_STEPS"
    )
    web_research_technical_max_live_rounds: int = Field(
        default=2, alias="WEB_RESEARCH_TECHNICAL_MAX_LIVE_ROUNDS"
    )

    # Email and auth integrations
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_from_address: str = ""

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True
    imap_inbox: str = "INBOX"

    # Google Calendar integration (optional)
    google_calendar_client_id: str = Field(
        default="", alias="GOOGLE_CALENDAR_CLIENT_ID"
    )
    google_calendar_client_secret: str = Field(
        default="", alias="GOOGLE_CALENDAR_CLIENT_SECRET"
    )
    google_calendar_redirect_uri: str = Field(
        default="", alias="GOOGLE_CALENDAR_REDIRECT_URI"
    )
    google_calendar_refresh_token: str = Field(
        default="", alias="GOOGLE_CALENDAR_REFRESH_TOKEN"
    )
    google_cse_api_key: str = Field(default="", alias="GOOGLE_CSE_API_KEY")
    google_cse_id: str = Field(default="", alias="GOOGLE_CSE_ID")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_search_model: str = Field(
        default="gemini-2.5-flash", alias="GEMINI_SEARCH_MODEL"
    )

    # Optional phone/SMS placeholders for future auth expansion
    sms_provider: str = ""
    sms_api_key: str = ""
    sms_sender_id: str = ""


settings = Settings()
