"""Outbound email transport. Only services.approvals may call `send_email`.

Providers (EMAIL_PROVIDER, per workspace):
- smtp (default): plain text through SMTP_HOST; dry-run (logged, not sent) when SMTP_HOST is blank.
- sendgrid / postmark: plain text + HTML, optional open/click tracking (EMAIL_TRACKING), and delivery events
  (bounces, complaints, opens, clicks) posted back to /api/webhooks/email/<provider>, which suppress bad
  addresses automatically (services/email_events.py). Each message carries its approval id so events
  map back to the draft.
"""
import html
import logging
import re
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

from ..config import settings
from ..integrations import http

log = logging.getLogger(__name__)

PROVIDERS = ("smtp", "sendgrid", "postmark")
_URL = re.compile(r"(https?://[^\s<>()\"']+)")


@dataclass
class SendResult:
    transport: str  # smtp|sendgrid|postmark|dry_run
    message_id: str | None = None


def active_provider() -> str:
    """The transport that would be used right now (shown on the dashboard)."""
    provider = (settings.email_provider or "smtp").lower()
    if provider == "sendgrid" and settings.sendgrid_api_key:
        return "sendgrid"
    if provider == "postmark" and settings.postmark_server_token:
        return "postmark"
    return "smtp" if settings.smtp_host else "dry_run"


def _tracking() -> bool:
    v = settings.email_tracking
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def _unsubscribe_header(to: str | None = None) -> str:
    """mailto plus, when PUBLIC_BASE_URL is set, a personal one-click URL (RFC 8058)."""
    mailto = f"<mailto:{settings.sender_email}?subject=unsubscribe>"
    if to:
        from ..services.privacy import unsubscribe_url

        if url := unsubscribe_url(to):
            return f"<{url}>, {mailto}"
    return mailto


def _one_click(to: str) -> bool:
    return "http" in _unsubscribe_header(to)


def to_html(text: str) -> str:
    """Plain text -> simple, readable HTML (paragraphs, line breaks, clickable links). No images or styling
    that would make a 1:1 email look like a newsletter."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out = []
    for p in paragraphs:
        escaped = html.escape(p)
        linked = _URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', escaped)
        out.append(f"<p>{linked.replace(chr(10), '<br>')}</p>")
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">'
            + "".join(out) + "</div>")


def send_internal(to: str, subject: str, body: str) -> str:
    """Notifications to our own team (digests, alerts). Never used for prospects."""
    provider = active_provider()
    if provider == "dry_run":
        log.info("internal notification (dry run) to %s: %s", to, subject)
        return "dry_run"
    if provider == "sendgrid":
        http.request("POST", "https://api.sendgrid.com/v3/mail/send", headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"},
                     json_body={"personalizations": [{"to": [{"email": to}]}], "from": {"email": settings.sender_email},
                                "subject": subject, "content": [{"type": "text/plain", "value": body}],
                                "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False}}})
        return "sendgrid"
    if provider == "postmark":
        http.request("POST", "https://api.postmarkapp.com/email", headers={"X-Postmark-Server-Token": settings.postmark_server_token,
                                                                           "Accept": "application/json"},
                     json_body={"From": settings.sender_email, "To": to, "Subject": subject, "TextBody": body,
                                "MessageStream": "outbound"})
        return "postmark"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.sender_email, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)
    return "smtp"


def send_email(to: str, subject: str, body: str, *, approval_id: int | None = None) -> SendResult:
    provider = active_provider()
    if provider == "sendgrid":
        return _sendgrid(to, subject, body, approval_id)
    if provider == "postmark":
        return _postmark(to, subject, body, approval_id)
    if provider == "dry_run":
        log.warning("Email not configured; dry-run send to %s: %s", to, subject)
        return SendResult("dry_run", f"dry-run-{uuid.uuid4().hex[:12]}")
    return _smtp(to, subject, body)


def _smtp(to: str, subject: str, body: str) -> SendResult:
    msg = EmailMessage()
    msg["From"] = settings.sender_email
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id = make_msgid(domain=(settings.sender_email or "localhost").split("@")[-1])
    msg["List-Unsubscribe"] = _unsubscribe_header(to)
    if _one_click(to):
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)
    return SendResult("smtp", message_id)


def _sendgrid(to: str, subject: str, body: str, approval_id: int | None) -> SendResult:
    track = _tracking()
    payload = {
        "personalizations": [{"to": [{"email": to}],
                              "custom_args": {"approval_id": str(approval_id)} if approval_id else {}}],
        "from": {"email": settings.sender_email, "name": settings.company_name},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}, {"type": "text/html", "value": to_html(body)}],
        "headers": {"List-Unsubscribe": _unsubscribe_header(to),
                    **({"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if _one_click(to) else {})},
        "tracking_settings": {"click_tracking": {"enable": track, "enable_text": False},
                              "open_tracking": {"enable": track}},
    }
    # 202 Accepted with an empty body; events reference our approval_id custom arg.
    http.request("POST", "https://api.sendgrid.com/v3/mail/send", json_body=payload,
                 headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"})
    return SendResult("sendgrid", f"approval-{approval_id}" if approval_id else None)


def _postmark(to: str, subject: str, body: str, approval_id: int | None) -> SendResult:
    track = _tracking()
    payload = {
        "From": f"{settings.company_name} <{settings.sender_email}>", "To": to, "Subject": subject,
        "TextBody": body, "HtmlBody": to_html(body), "MessageStream": "outbound",
        "TrackOpens": track, "TrackLinks": "HtmlAndText" if track else "None",
        "Headers": [{"Name": "List-Unsubscribe", "Value": _unsubscribe_header(to)}]
                   + ([{"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"}] if _one_click(to) else []),
        "Metadata": {"approval_id": str(approval_id)} if approval_id else {},
    }
    data = http.request("POST", "https://api.postmarkapp.com/email", json_body=payload,
                        headers={"X-Postmark-Server-Token": settings.postmark_server_token,
                                 "Accept": "application/json"})
    return SendResult("postmark", data.get("MessageID"))
