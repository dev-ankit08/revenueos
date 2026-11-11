"""Applying data providers to contacts: enrichment (fill gaps) and email verification (cached per contact)."""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Activity, Contact
from ..tools import dataproviders as dp

log = logging.getLogger(__name__)
EMAIL_FORMAT = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
RECHECK_AFTER = timedelta(days=90)


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def enrich_contact(db: Session, c: Contact) -> list[str]:
    """Fill empty fields from the enrichment provider. Returns the fields filled."""
    acc = c.account
    try:
        e = dp.enrich_person(email=c.email, first_name=c.first_name, last_name=c.last_name,
                             company=acc.name, domain=acc.domain)
    except Exception as exc:
        log.warning("enrichment failed for contact %s: %s", c.id, exc)
        e = None
    c.enriched_at = datetime.now(timezone.utc)
    if not e:
        return []
    filled = []
    for obj, attr, value in ((c, "title", e.title), (c, "linkedin_url", e.linkedin_url), (c, "timezone", e.timezone),
                             (acc, "domain", e.company_domain), (acc, "industry", e.industry),
                             (acc, "employees", e.employees), (acc, "country", e.country)):
        if value and not getattr(obj, attr):
            setattr(obj, attr, value)
            filled.append(attr)
    if filled:
        db.add(Activity(contact_id=c.id, type="enriched",
                        detail=f"Filled from {dp.enrichment_provider()}: {', '.join(filled)}"))
    return filled


def enrich_pending(db: Session, limit: int = 25) -> int:
    """Contacts never enriched, newest first (called every scheduler tick when a provider is set)."""
    if not dp.enrichment_provider():
        return 0
    n = 0
    for c in db.scalars(select(Contact).where(Contact.enriched_at.is_(None)).order_by(Contact.id.desc()).limit(limit)):
        n += bool(enrich_contact(db, c))
    return n


def email_check(db: Session, c: Contact) -> tuple[str, str]:
    """(status, detail) for the contact's address: format first, then the provider (cached for 90 days)."""
    if not c.email or not EMAIL_FORMAT.match(c.email.strip()):
        return dp.INVALID, "malformed address"
    if not dp.verification_provider():
        return dp.UNKNOWN, "no verification provider"
    checked = _aware(c.email_checked_at)
    if c.email_status and checked and datetime.now(timezone.utc) - checked < RECHECK_AFTER:
        return c.email_status, c.email_status_detail or ""
    try:
        status, detail = dp.verify_email(c.email.strip())
    except Exception as exc:  # provider down: don't block outreach on it
        log.warning("email verification failed for %s: %s", c.email, exc)
        return dp.UNKNOWN, f"verification unavailable ({type(exc).__name__})"
    c.email_status, c.email_status_detail, c.email_checked_at = status, detail, datetime.now(timezone.utc)
    return status, detail
