"""Outbound email transport. Only services.approvals may call `send_email`."""
import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> str:
    """Send one plain-text email. Returns 'smtp' or 'dry_run'."""
    if not settings.smtp_host:
        log.warning("SMTP not configured; dry-run send to %s: %s", to, subject)
        return "dry_run"

    msg = EmailMessage()
    msg["From"] = settings.sender_email
    msg["To"] = to
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:{settings.sender_email}?subject=unsubscribe>"
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)
    return "smtp"
