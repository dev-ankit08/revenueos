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
    transport: str  # smtp|sendgrid|postmark|gmail|outlook|dry_run
    message_id: str | None = None


@dataclass
class Sender:
    """Who an email is sent as. provider google/microsoft sends through that person's own mailbox (token)."""
    address: str
    provider: str = "smtp"
    token: str | None = None


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


def send_email(to: str, subject: str, body: str, *, approval_id: int | None = None,
               sender: Sender | None = None) -> SendResult:
    """Send one approved email. `sender` picks the mailbox from the sending pool (default: SENDER_EMAIL)."""
    if sender and sender.provider == "google":
        return _gmail_api(to, subject, body, sender)
    if sender and sender.provider == "microsoft":
        return _graph_api(to, subject, body, sender)
    from_address = sender.address if sender else settings.sender_email
    provider = active_provider()
    if provider == "sendgrid":
        return _sendgrid(to, subject, body, approval_id, from_address)
    if provider == "postmark":
        return _postmark(to, subject, body, approval_id, from_address)
    if provider == "dry_run":
        log.warning("Email not configured; dry-run send from %s to %s: %s", from_address, to, subject)
        return SendResult("dry_run", f"dry-run-{uuid.uuid4().hex[:12]}")
    return _smtp(to, subject, body, from_address)


def send_from_mailbox(db, mailbox, to: str, subject: str, body: str) -> SendResult:
    """Internal test sends from a pool mailbox (inbox placement tests); not for prospects."""
    return send_email(to, subject, body, sender=sender_for(db, mailbox))


def sender_for(db, mailbox) -> Sender:
    if mailbox.provider in ("google", "microsoft") and mailbox.connection_id:
        from ..integrations import oauth
        from ..models import Connection

        conn = db.get(Connection, mailbox.connection_id)
        if not conn or conn.status != "active":
            raise RuntimeError(f"The connected mailbox for {mailbox.address} is disconnected. Reconnect it on Integrations.")
        return Sender(mailbox.address, mailbox.provider, oauth.access_token(db, conn))
    return Sender(mailbox.address, "smtp")


def _rfc822(to: str, subject: str, body: str, from_address: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = from_address, to, subject
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])
    msg["List-Unsubscribe"] = _unsubscribe_header(to)
    if _one_click(to):
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    return msg


def _gmail_api(to: str, subject: str, body: str, sender: Sender) -> SendResult:
    import base64

    raw = base64.urlsafe_b64encode(_rfc822(to, subject, body, sender.address).as_bytes()).decode()
    data = http.request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                        headers={"Authorization": f"Bearer {sender.token}"}, json_body={"raw": raw})
    return SendResult("gmail", (data or {}).get("id"))


def _graph_api(to: str, subject: str, body: str, sender: Sender) -> SendResult:
    # Graph only allows X- custom headers, so the unsubscribe link travels in the footer (always added).
    http.request("POST", "https://graph.microsoft.com/v1.0/me/sendMail",
                 headers={"Authorization": f"Bearer {sender.token}"},
                 json_body={"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                                        "toRecipients": [{"emailAddress": {"address": to}}]},
                            "saveToSentItems": True})
    return SendResult("outlook", None)


def _smtp(to: str, subject: str, body: str, from_address: str | None = None) -> SendResult:
    msg = EmailMessage()
    msg["From"] = from_address or settings.sender_email
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


def _sendgrid(to: str, subject: str, body: str, approval_id: int | None, from_address: str | None = None) -> SendResult:
    track = _tracking()
    payload = {
        "personalizations": [{"to": [{"email": to}],
                              "custom_args": {"approval_id": str(approval_id)} if approval_id else {}}],
        "from": {"email": from_address or settings.sender_email, "name": settings.company_name},
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


def _postmark(to: str, subject: str, body: str, approval_id: int | None, from_address: str | None = None) -> SendResult:
    track = _tracking()
    payload = {
        "From": f"{settings.company_name} <{from_address or settings.sender_email}>", "To": to, "Subject": subject,
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
