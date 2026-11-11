"""Runtime configuration, read once from environment variables."""
import os
from dataclasses import dataclass, field
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# Local runs read revenueos/.env (Docker passes it as env_file). Real environment variables win.
if not os.getenv("REVENUEOS_NO_DOTENV"):
    from dotenv import load_dotenv

    load_dotenv(_BACKEND_DIR.parent / ".env", override=False)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    company_name: str = os.getenv("COMPANY_NAME", "Your Company")
    brand_color: str = os.getenv("BRAND_COLOR", "3B5BDB")  # hex, used in generated decks/proposals
    generated_dir: Path = Path(os.getenv("GENERATED_DIR", str(_BACKEND_DIR / "generated")))
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{_BACKEND_DIR / 'revenueos.db'}")

    # Model tiering: orchestration/strategy -> Opus, content/research -> Sonnet,
    # high-volume classification/scoring -> Haiku.
    model_orchestrator: str = os.getenv("MODEL_ORCHESTRATOR", "claude-opus-5-5")
    model_worker: str = os.getenv("MODEL_WORKER", "claude-sonnet-5-5")
    model_fast: str = os.getenv("MODEL_FAST", "claude-haiku-5-5")
    # Automatic retries (with backoff) on rate limits, overload and network errors from the Claude API.
    claude_max_retries: int = _int("CLAUDE_MAX_RETRIES", 4)
    # Knowledge-base search: a fast model re-ranks keyword matches so the best proof point comes first.
    kb_rerank: bool = os.getenv("KB_RERANK", "true").lower() in ("1", "true", "yes")
    # Research older than this is refreshed before writing outreach (days).
    brief_max_age_days: int = _int("BRIEF_MAX_AGE_DAYS", 30)
    # Learning loop: minimum evidence before it proposes a playbook, and whether proposals go live unreviewed.
    learn_min_sent: int = _int("LEARN_MIN_SENT", 50)
    learn_min_replies: int = _int("LEARN_MIN_REPLIES", 10)
    learn_auto_publish: bool = os.getenv("LEARN_AUTO_PUBLISH", "false").lower() in ("1", "true", "yes")
    dossier_max_age_days: int = _int("DOSSIER_MAX_AGE_DAYS", 90)

    # Salesforce. If unset, the built-in local CRM is used instead.
    sf_username: str | None = os.getenv("SF_USERNAME")
    sf_password: str | None = os.getenv("SF_PASSWORD")
    sf_security_token: str | None = os.getenv("SF_SECURITY_TOKEN")
    sf_domain: str = os.getenv("SF_DOMAIN", "login")  # "test" for sandboxes

    # Which CRM is the system of record: local | salesforce | hubspot | dynamics.
    # Default: salesforce when SF_* credentials are set, else local.
    crm_provider: str = os.getenv("CRM_PROVIDER", "")
    hubspot_token: str | None = os.getenv("HUBSPOT_TOKEN")  # private app access token
    dynamics_url: str | None = os.getenv("DYNAMICS_URL")  # e.g. https://yourorg.crm.dynamics.com
    dynamics_client_id: str | None = os.getenv("DYNAMICS_CLIENT_ID") or os.getenv("MS_CLIENT_ID")
    dynamics_client_secret: str | None = os.getenv("DYNAMICS_CLIENT_SECRET") or os.getenv("MS_CLIENT_SECRET")
    # Optional JSON overriding stage/status mappings per CRM, e.g. {"deal_stage": {"proposal": "contractsent"}}
    crm_field_map: str = os.getenv("CRM_FIELD_MAP", "{}")
    # Zoho CRM (self-client OAuth refresh token) and Pipedrive (API token)
    zoho_client_id: str | None = os.getenv("ZOHO_CLIENT_ID")
    zoho_client_secret: str | None = os.getenv("ZOHO_CLIENT_SECRET")
    zoho_refresh_token: str | None = os.getenv("ZOHO_REFRESH_TOKEN")
    zoho_accounts_url: str = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")  # .eu / .in / .com.au
    zoho_api_domain: str = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.com")
    pipedrive_token: str | None = os.getenv("PIPEDRIVE_TOKEN")
    pipedrive_domain: str = os.getenv("PIPEDRIVE_DOMAIN", "api.pipedrive.com")  # or yourcompany.pipedrive.com

    # Outbound email. If SMTP_HOST is unset, approved emails are logged, not sent.
    smtp_host: str | None = os.getenv("SMTP_HOST")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str | None = os.getenv("SMTP_USER")
    smtp_password: str | None = os.getenv("SMTP_PASSWORD")
    sender_email: str = os.getenv("SENDER_EMAIL", "sales@example.com")
    physical_address: str = os.getenv("PHYSICAL_ADDRESS", "123 Example St, City, Country")

    # Scheduled signal scans cost AI + web-search spend, so they are opt-in.
    # 0 = off; otherwise scan the top accounts every N days.
    signal_scan_interval_days: int = _int("SIGNAL_SCAN_INTERVAL_DAYS", 0)

    # Quarterly revenue target for the forecast (0 = not set).
    quarterly_target: float = float(os.getenv("QUARTERLY_TARGET", "0"))
    # Automatic weekly-style revenue review (deal reviews + forecast + learning loop); costs AI spend.
    # 0 = off; otherwise run every N days.
    review_interval_days: int = _int("REVIEW_INTERVAL_DAYS", 0)

    # Where this API and the web app are reachable (OAuth redirects, links in Slack messages).
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:3000")
    # Encrypts stored OAuth tokens. Required before connecting any mailbox or calendar.
    secret_key: str | None = os.getenv("SECRET_KEY")

    # Integrations (each is off until its secret is set)
    google_client_id: str | None = os.getenv("GOOGLE_CLIENT_ID")
    google_client_secret: str | None = os.getenv("GOOGLE_CLIENT_SECRET")
    ms_client_id: str | None = os.getenv("MS_CLIENT_ID")
    ms_client_secret: str | None = os.getenv("MS_CLIENT_SECRET")
    ms_tenant: str = os.getenv("MS_TENANT", "common")
    gong_access_key: str | None = os.getenv("GONG_ACCESS_KEY")
    gong_access_secret: str | None = os.getenv("GONG_ACCESS_SECRET")
    gong_base_url: str = os.getenv("GONG_BASE_URL", "https://api.gong.io")
    slack_bot_token: str | None = os.getenv("SLACK_BOT_TOKEN")
    slack_approvals_channel: str | None = os.getenv("SLACK_APPROVALS_CHANNEL")
    # Share of eligible outbound leads randomly held back (no AI outreach) to measure lift.
    holdout_pct: float = float(os.getenv("HOLDOUT_PCT", "0.2"))
    slack_signing_secret: str | None = os.getenv("SLACK_SIGNING_SECRET")
    zoom_webhook_secret: str | None = os.getenv("ZOOM_WEBHOOK_SECRET")
    # Raw Slack/call/email text is deleted after this many days; summaries are kept.
    context_retention_days: int = _int("CONTEXT_RETENTION_DAYS", 90)
    meeting_prep_hours_before: int = _int("MEETING_PREP_HOURS_BEFORE", 24)
    # Drafts scoring below this (0-100) are sent back once for a more personal rewrite.
    personalisation_threshold: int = _int("PERSONALISATION_THRESHOLD", 60)

    # Guardrails
    daily_send_cap: int = _int("DAILY_SEND_CAP", 200)
    per_domain_daily_cap: int = _int("PER_DOMAIN_DAILY_CAP", 3)

    # Email delivery: "smtp" (default; dry-run when SMTP_HOST is blank), "sendgrid" or "postmark".
    # Sending services add HTML, open/click tracking and bounce/complaint webhooks.
    email_provider: str = os.getenv("EMAIL_PROVIDER", "smtp").lower()
    sendgrid_api_key: str | None = os.getenv("SENDGRID_API_KEY")
    postmark_server_token: str | None = os.getenv("POSTMARK_SERVER_TOKEN")
    # SendGrid signed event webhook public key (base64). When unset, events need ?token=<WEBHOOK_SECRET>.
    sendgrid_webhook_public_key: str | None = os.getenv("SENDGRID_WEBHOOK_PUBLIC_KEY")
    email_tracking: bool = os.getenv("EMAIL_TRACKING", "true").lower() in ("1", "true", "yes")
    # Sequences send only inside this window, in the contact's time zone (else DEFAULT_TIMEZONE).
    send_window_start_hour: int = _int("SEND_WINDOW_START_HOUR", 8)
    send_window_end_hour: int = _int("SEND_WINDOW_END_HOUR", 18)
    send_weekdays_only: bool = os.getenv("SEND_WEEKDAYS_ONLY", "true").lower() in ("1", "true", "yes")
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
    # Meeting booking link (Calendly, Microsoft Bookings, Google appointment page) offered in replies
    # to interested prospects.
    booking_url: str | None = os.getenv("BOOKING_URL")
    # UTM tags on links to your own site in sent emails and approved content. Domains to tag
    # (comma-separated); the sender email's domain is always included.
    utm_enabled: bool = os.getenv("UTM_ENABLED", "true").lower() in ("1", "true", "yes")
    utm_domains: str = os.getenv("UTM_DOMAINS", "")
    # Data providers (per workspace). Enrichment: "apollo". Email verification: "zerobounce" or "neverbounce".
    enrichment_provider: str | None = os.getenv("ENRICHMENT_PROVIDER")
    apollo_api_key: str | None = os.getenv("APOLLO_API_KEY")
    verification_provider: str | None = os.getenv("VERIFICATION_PROVIDER")
    zerobounce_api_key: str | None = os.getenv("ZEROBOUNCE_API_KEY")
    neverbounce_api_key: str | None = os.getenv("NEVERBOUNCE_API_KEY")
    # Notifications: daily digest hour (local, DEFAULT_TIMEZONE; -1 = off) and where ops alerts go.
    digest_hour: int = _int("DIGEST_HOUR", 8)
    alert_email: str | None = os.getenv("ALERT_EMAIL")
    # Privacy: countries where a lawful basis must be recorded before emailing (comma-separated, as stored on
    # accounts, e.g. "Germany,DE,France,FR"); retention of AI run logs and rejected drafts (days).
    consent_required_countries: str = os.getenv("CONSENT_REQUIRED_COUNTRIES", "")
    run_retention_days: int = _int("RUN_RETENTION_DAYS", 180)
    draft_retention_days: int = _int("DRAFT_RETENTION_DAYS", 365)
    # Website tracking and intent: pages that signal buying intent when a known contact views them; whether a strong
    # intent surge on an unknown company creates the account.
    intent_pages: str = os.getenv("INTENT_PAGES", "/pricing,/demo,/contact,/request-a-demo,/plans")
    intent_create_accounts: bool = os.getenv("INTENT_CREATE_ACCOUNTS", "false").lower() in ("1", "true", "yes")
    # Events: attendees who stayed this long (minutes) or asked a question get a follow-up task.
    event_engaged_minutes: int = _int("EVENT_ENGAGED_MINUTES", 30)
    # Customers: open a renewal deal this many days before a contract ends.
    renewal_lead_days: int = _int("RENEWAL_LEAD_DAYS", 90)
    # LinkedIn publishing (company page or member).
    linkedin_access_token: str | None = os.getenv("LINKEDIN_ACCESS_TOKEN")
    linkedin_author_urn: str | None = os.getenv("LINKEDIN_AUTHOR_URN")
    # Lifecycle: a lead becomes an MQL at this score once they have engaged (reply, click, inbound form).
    mql_score: int = _int("MQL_SCORE", 70)
    # Hours allowed between becoming an MQL and the first human touch.
    mql_sla_hours: int = _int("MQL_SLA_HOURS", 24)

    # Single sign-on (OpenID Connect: Microsoft Entra ID, Google Workspace, Okta...). Users must already exist
    # unless OIDC_AUTO_PROVISION=true (new users join the default workspace as members).
    oidc_issuer: str | None = os.getenv("OIDC_ISSUER")
    oidc_client_id: str | None = os.getenv("OIDC_CLIENT_ID")
    oidc_client_secret: str | None = os.getenv("OIDC_CLIENT_SECRET")
    oidc_auto_provision: bool = os.getenv("OIDC_AUTO_PROVISION", "false").lower() in ("1", "true", "yes")
    # Error tracking (optional; needs the sentry-sdk package).
    sentry_dsn: str | None = os.getenv("SENTRY_DSN")
    # Key rotation: put the previous SECRET_KEY here while re-encrypting (python -m revenueos.rotate_keys).
    secret_key_old: str | None = os.getenv("SECRET_KEY_OLD")

    # If set, every /api request needs "Authorization: Bearer <API_TOKEN>"
    # and webhooks need "X-Webhook-Secret: <WEBHOOK_SECRET>".
    api_token: str | None = os.getenv("API_TOKEN")
    webhook_secret: str | None = os.getenv("WEBHOOK_SECRET")

    cors_origins: list[str] = field(
        default_factory=lambda: os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    )

    @property
    def salesforce_enabled(self) -> bool:
        return bool(self.sf_username and self.sf_password)

    @property
    def crm(self) -> str:
        return self.crm_provider or ("salesforce" if self.salesforce_enabled else "local")


# Settings each client workspace may set for itself (Settings page). Everything else is platform-wide.
WORKSPACE_KEYS = {
    "company_name", "brand_color", "physical_address", "sender_email",
    "crm_provider", "crm_field_map", "hubspot_token", "dynamics_url", "dynamics_client_id", "dynamics_client_secret",
    "sf_username", "sf_password", "sf_security_token", "sf_domain",
    "zoho_client_id", "zoho_client_secret", "zoho_refresh_token", "zoho_accounts_url", "zoho_api_domain",
    "pipedrive_token", "pipedrive_domain",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password", "daily_send_cap", "per_domain_daily_cap",
    "slack_signing_secret", "slack_bot_token", "slack_approvals_channel", "zoom_webhook_secret", "webhook_secret",
    "gong_access_key", "gong_access_secret", "holdout_pct", "quarterly_target", "personalisation_threshold",
    "signal_scan_interval_days", "review_interval_days",
    "email_provider", "sendgrid_api_key", "postmark_server_token", "email_tracking", "sendgrid_webhook_public_key",
    "send_window_start_hour", "send_window_end_hour", "send_weekdays_only", "default_timezone",
    "booking_url", "utm_enabled", "utm_domains", "mql_score", "mql_sla_hours",
    "brief_max_age_days", "dossier_max_age_days",
    "enrichment_provider", "apollo_api_key", "verification_provider", "zerobounce_api_key", "neverbounce_api_key",
    "digest_hour", "alert_email", "consent_required_countries",
    "intent_pages", "intent_create_accounts", "event_engaged_minutes", "renewal_lead_days",
    "linkedin_access_token", "linkedin_author_urn",
}
# Stored encrypted; never returned in full by the API.
SECRET_KEYS = {"hubspot_token", "dynamics_client_secret", "sf_password", "sf_security_token", "zoho_client_secret",
               "zoho_refresh_token", "pipedrive_token", "smtp_password", "slack_signing_secret", "slack_bot_token",
               "zoom_webhook_secret", "webhook_secret", "gong_access_secret", "sendgrid_api_key",
               "postmark_server_token", "apollo_api_key", "zerobounce_api_key", "neverbounce_api_key",
               "linkedin_access_token"}


class SettingsProxy:
    """`settings` everywhere: the deployment's Settings, overlaid with the current workspace's own values.

    The workspace comes from a context variable set per request/job (see db.current_workspace);
    services/workspaces.py installs the lookup. With no workspaces, it is exactly the env settings.
    """

    def __init__(self, base: Settings):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", None)  # callable -> dict for the current workspace

    def __getattr__(self, name: str):
        if name in WORKSPACE_KEYS and self._overrides is not None:
            values = self._overrides()
            if name in values and values[name] not in (None, ""):
                return values[name]
        return getattr(self._base, name)

    @property
    def salesforce_enabled(self) -> bool:
        return bool(self.sf_username and self.sf_password)

    @property
    def crm(self) -> str:
        return self.crm_provider or ("salesforce" if self.salesforce_enabled else "local")


settings = SettingsProxy(Settings())
