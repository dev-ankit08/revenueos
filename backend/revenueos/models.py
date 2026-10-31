"""Database tables.

Accounts and contacts mirror Salesforce records (sf_id) so agents work from one
local, fast copy; the Salesforce adapter keeps them in sync.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    sf_id: Mapped[str | None] = mapped_column(String(18), index=True)
    crm_id: Mapped[str | None] = mapped_column(String(64), index=True)  # HubSpot / Dynamics id
    name: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    industry: Mapped[str | None] = mapped_column(String(120))
    employees: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(40))  # manual|salesforce|sourced|inbound|import
    fit_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="account")
    briefs: Mapped[list["AccountBrief"]] = relationship(
        back_populates="account", order_by="AccountBrief.created_at.desc()"
    )


class Contact(Base):
    """A lead or contact; `status` follows the pipeline stage."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    sf_id: Mapped[str | None] = mapped_column(String(18), index=True)
    crm_id: Mapped[str | None] = mapped_column(String(64), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(40), default="manual")  # manual|inbound|salesforce|sourced
    status: Mapped[str] = mapped_column(String(40), default="new")
    # new|researched|sequenced|contacted|replied|meeting|disqualified
    score: Mapped[float | None] = mapped_column(Float)
    score_reason: Mapped[str | None] = mapped_column(Text)
    score_tier: Mapped[str | None] = mapped_column(String(1))  # A|B|C|D
    owner: Mapped[str | None] = mapped_column(String(120))
    inbound_message: Mapped[str | None] = mapped_column(Text)
    # Lift measurement: "treatment" gets AI outreach, "holdout" is randomly held back.
    experiment_group: Mapped[str | None] = mapped_column(String(10))
    experiment_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="contacts")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class AccountBrief(Base):
    """Output of the Account Researcher; the grounding for all outreach."""

    __tablename__ = "account_briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    summary: Mapped[str] = mapped_column(Text)
    priorities: Mapped[list] = mapped_column(JSON, default=list)
    pain_points: Mapped[list] = mapped_column(JSON, default=list)
    recent_news: Mapped[list] = mapped_column(JSON, default=list)  # [{headline, url, date}]
    recommended_angle: Mapped[str] = mapped_column(Text, default="")
    relevant_proof_points: Mapped[list] = mapped_column(JSON, default=list)
    likely_objections: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="briefs")


class KBDocument(Base):
    """Knowledge base: what we sell, proof points, ICP, brand voice, competitors."""

    __tablename__ = "kb_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(40), index=True)
    # offering|case_study|icp|persona|brand_voice|competitor|pricing|objection|other
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chunks: Mapped[list["KBChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KBChunk(Base):
    __tablename__ = "kb_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("kb_documents.id"))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)

    document: Mapped[KBDocument] = relationship(back_populates="chunks")


class Approval(Base):
    """Everything an agent wants to send or publish waits here for a human."""

    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))  # email|linkedin_message|content
    content_type: Mapped[str | None] = mapped_column(String(40))
    # for kind=content: blog_post|linkedin_post|ad|landing_page|newsletter|case_study|one_pager|social_post
    title: Mapped[str | None] = mapped_column(String(255))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending|approved|rejected|sent|failed
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    sequence_step: Mapped[int] = mapped_column(Integer, default=1)
    send_after_days: Mapped[int] = mapped_column(Integer, default=0)
    variant: Mapped[str] = mapped_column(String(4), default="A")
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    guardrail_flags: Mapped[list] = mapped_column(JSON, default=list)
    personalisation_score: Mapped[int | None] = mapped_column(Integer)
    personalisation_notes: Mapped[dict | None] = mapped_column(JSON)
    claims: Mapped[list | None] = mapped_column(JSON)  # [{text, source}] the writer cited
    created_by_agent: Mapped[str] = mapped_column(String(60))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    contact: Mapped[Contact | None] = relationship()


class Suppression(Base):
    """Unsubscribes, bounces and do-not-contact entries. Never contacted again."""

    __tablename__ = "suppressions"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_or_domain: Mapped[str] = mapped_column(String(255), unique=True)
    reason: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    """Audit log + cost tracking for every agent invocation."""

    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(60), index=True)
    parent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|succeeded|failed
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Activity(Base):
    """Timeline of what happened to a contact (sent, replied, scored...)."""

    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Opportunity(Base):
    """A deal. Created or updated by the Call Analyst; mirrors a Salesforce Opportunity."""

    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(primary_key=True)
    sf_id: Mapped[str | None] = mapped_column(String(18), index=True)
    crm_id: Mapped[str | None] = mapped_column(String(64), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    primary_contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    name: Mapped[str] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(String(40), default="discovery")
    # discovery|qualification|solution|proposal|negotiation|closed_won|closed_lost
    amount: Mapped[float | None] = mapped_column(Float)
    close_date: Mapped[str | None] = mapped_column(String(10))  # ISO date
    qualification: Mapped[dict] = mapped_column(JSON, default=dict)  # MEDDICC fields
    next_steps: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    health: Mapped[str | None] = mapped_column(String(10))  # green|amber|red
    win_probability: Mapped[float | None] = mapped_column(Float)  # 0-100, from the Deal Strategist
    forecast_category: Mapped[str | None] = mapped_column(String(12))  # commit|best_case|pipeline|omitted
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()
    primary_contact: Mapped[Contact | None] = relationship()
    stage_changes: Mapped[list["StageChange"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan", order_by="StageChange.id"
    )


class StageChange(Base):
    """Deal stage history; written automatically whenever Opportunity.stage changes."""

    __tablename__ = "stage_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    from_stage: Mapped[str | None] = mapped_column(String(40))
    to_stage: Mapped[str] = mapped_column(String(40))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    opportunity: Mapped[Opportunity] = relationship(back_populates="stage_changes")


@event.listens_for(Opportunity.stage, "set")
def _record_stage_change(target: Opportunity, value, oldvalue, initiator):
    old = oldvalue if isinstance(oldvalue, str) else None
    if value == old:
        return
    target.stage_changes.append(StageChange(from_stage=old, to_stage=value))
    if value in ("closed_won", "closed_lost"):
        target.closed_at = utcnow()


class SalesDocument(Base):
    """Agent-produced documents: meeting prep, call summaries, proposals, decks, hygiene reports."""

    __tablename__ = "sales_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    # meeting_prep|call_summary|proposal|pitch_deck|hygiene_report|reply_triage
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)  # Markdown
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    file_name: Mapped[str | None] = mapped_column(String(255))  # generated .pptx/.docx, if any
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    """A marketing campaign planned by the Campaign Strategist."""

    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    sf_id: Mapped[str | None] = mapped_column(String(18))
    crm_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|active|completed
    budget: Mapped[float | None] = mapped_column(Float)
    start_date: Mapped[str | None] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10))
    plan: Mapped[dict] = mapped_column(JSON, default=dict)  # CampaignPlan
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="targeted")  # targeted|engaged|converted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Signal(Base):
    """A buying trigger spotted by the Signal Detector (new exec, funding, M&A, hiring...)."""

    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    headline: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    event_date: Mapped[str | None] = mapped_column(String(10))
    strength: Mapped[str] = mapped_column(String(10))  # high|medium|low
    recommended_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new|actioned|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship()


class ContextEvent(Base):
    """One thing the team learned about a customer, from any source.

    The per-account timeline built from these is what every agent reads (services/memory.py).
    `internal` marks text customers must never see (internal Slack discussion): it informs
    strategy but the guardrail blocks it from outbound drafts.
    """

    __tablename__ = "context_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)  # slack|call|email|calendar|crm|note
    source_ref: Mapped[str] = mapped_column(String(255), unique=True)  # external id, for de-duplication
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    author: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    raw_text: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)  # commitments, objections, stakeholders, risks
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)  # summarised / acted on
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account | None] = relationship()


class SlackChannel(Base):
    """Opt-in list: RevenueOS reads only channels registered here."""

    __tablename__ = "slack_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))  # None = detect from text
    shared: Mapped[bool] = mapped_column(Boolean, default=False)  # Slack Connect channel with the customer
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalendarEvent(Base):
    """A meeting from a connected calendar; customer meetings get an automatic prep brief."""

    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attendees: Mapped[list] = mapped_column(JSON, default=list)  # email addresses
    meeting_url: Mapped[str | None] = mapped_column(String(1000))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    prep_status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|running|done|skipped|failed
    platform: Mapped[str | None] = mapped_column(String(12))  # zoom|teams|meet|other
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"))
    # After the meeting: fetch its transcript from Teams/Meet. none|pending|done|unavailable
    transcript_status: Mapped[str | None] = mapped_column(String(12))
    prep_document_id: Mapped[int | None] = mapped_column(ForeignKey("sales_documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Connection(Base):
    """An OAuth or API-key connection to a mailbox/calendar (Google, Microsoft) or a call tool (Gong).

    Tokens are encrypted with SECRET_KEY (services/secrets.py); never stored in clear text.
    """

    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))  # google|microsoft|gong
    account_email: Mapped[str | None] = mapped_column(String(255))
    scopes: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursors: Mapped[dict] = mapped_column(JSON, default=dict)  # last sync points per feed
    status: Mapped[str] = mapped_column(String(12), default="active")  # active|error|disabled
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PersonProfile(Base):
    """Person Researcher's dossier: the person's PUBLIC, PROFESSIONAL footprint, every fact sourced."""

    __tablename__ = "person_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), unique=True)
    summary: Mapped[str] = mapped_column(Text)
    role_history: Mapped[list] = mapped_column(JSON, default=list)
    public_statements: Mapped[list] = mapped_column(JSON, default=list)  # [{statement, source, date}]
    priorities: Mapped[list] = mapped_column(JSON, default=list)
    talking_points: Mapped[list] = mapped_column(JSON, default=list)
    communication_style: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentSetting(Base):
    """Governance per agent ("*" = all agents): model override, monthly budget, on/off."""

    __tablename__ = "agent_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(60), unique=True)
    model: Mapped[str | None] = mapped_column(String(60))
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    alerted_month: Mapped[str | None] = mapped_column(String(7))  # "2026-10": 80% warning already sent
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- platform (default database only)

from .db import ControlBase  # noqa: E402


class Workspace(ControlBase):
    """A client (or the default company). Each has its own database and settings."""

    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    db_url: Mapped[str | None] = mapped_column(String(500))
    db_schema: Mapped[str | None] = mapped_column(String(80))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # WORKSPACE_KEYS overrides; secrets encrypted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(ControlBase):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    role: Mapped[str] = mapped_column(String(12), default="member")  # owner (platform) | admin | member
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(ControlBase):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the bearer token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evaluation(Base):
    """Blind test: RevenueOS drafts vs the incumbent (human-written or another AI) for the same leads."""

    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    incumbent_label: Mapped[str] = mapped_column(String(120), default="Current approach")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalItem(Base):
    __tablename__ = "eval_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    revenueos_subject: Mapped[str | None] = mapped_column(String(255))
    revenueos_body: Mapped[str] = mapped_column(Text)
    incumbent_subject: Mapped[str | None] = mapped_column(String(255))
    incumbent_body: Mapped[str] = mapped_column(Text)
    revenueos_is_a: Mapped[bool] = mapped_column(Boolean)  # random side assignment, hidden from raters


class EvalRating(Base):
    __tablename__ = "eval_ratings"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("eval_items.id"), index=True)
    rater: Mapped[str] = mapped_column(String(120))
    choice: Mapped[str] = mapped_column(String(4))  # A | B | tie
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
