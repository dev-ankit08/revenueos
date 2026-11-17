"""Privacy: lawful basis per contact, one-click unsubscribe (public), data-subject export and erasure (admin)."""
import html
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Contact
from ..services import privacy, sequences
from ..services.approvals import audit
from ..services.inbound import suppress
from .workspace import caller, require_admin

router = APIRouter(prefix="/api", tags=["privacy"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


class BasisIn(BaseModel):
    lawful_basis: str | None  # consent | legitimate_interest | contract | None
    source: str | None = None


@router.put("/leads/{contact_id}/lawful-basis")
def set_basis(contact_id: int, body: BasisIn, request: Request, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    if body.lawful_basis and body.lawful_basis not in privacy.BASES:
        raise HTTPException(422, f"lawful_basis must be one of {', '.join(privacy.BASES)}")
    c.lawful_basis, c.consent_source = body.lawful_basis, body.source
    c.consent_at = datetime.now(timezone.utc) if body.lawful_basis else None
    audit(db, _actor(request), "privacy.basis", "contact", c.id, basis=body.lawful_basis, source=body.source)
    db.commit()
    return {"contact_id": c.id, "lawful_basis": c.lawful_basis, "consent_source": c.consent_source}


class EmailIn(BaseModel):
    email: str


@router.post("/privacy/export", dependencies=[Depends(require_admin)])
def export(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    """Everything held about this email address (data-subject access request)."""
    out = privacy.export(db, body.email)
    audit(db, _actor(request), "privacy.export", "email", body.email.lower(), found=out["found"])
    db.commit()
    return out


@router.post("/privacy/erase", dependencies=[Depends(require_admin)])
def erase(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    """Delete this person's data; only a suppression entry remains so they are never contacted again."""
    out = privacy.erase(db, body.email)
    audit(db, _actor(request), "privacy.erase", "email", body.email.lower(), **out)
    db.commit()
    return out


# ------------------------------------------------------------------ public one-click unsubscribe

def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title><style>body{{font-family:Arial,sans-serif;max-width:520px;margin:60px auto;padding:0 16px;color:#222}}
button{{font-size:16px;padding:10px 18px;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer}}</style></head>
<body><h2>{html.escape(title)}</h2>{body}<p style="color:#777;font-size:13px">{html.escape(settings.company_name)} · {html.escape(settings.physical_address or '')}</p></body></html>""")


def _unsubscribe(db: Session, token: str) -> str | None:
    email = privacy.read_unsubscribe_token(token)
    if not email:
        return None
    suppress(db, email, "unsubscribed via link")
    from sqlalchemy import select

    for c in db.scalars(select(Contact).where(Contact.email.ilike(email))):
        sequences.stop_contact(db, c.id, "Unsubscribed via link.")
        if c.status not in ("meeting",):
            c.status = "disqualified"
    db.commit()
    return email


@router.get("/public/unsubscribe")
def unsubscribe_page(t: str):
    if not privacy.read_unsubscribe_token(t):
        return _page("Link not valid", "<p>This unsubscribe link isn't valid. Reply 'unsubscribe' to any email instead.</p>")
    return _page("Unsubscribe", f"""<p>Stop all emails from {html.escape(settings.company_name)}?</p>
<form method="post"><input type="hidden" name="t" value="{html.escape(t)}"><button type="submit">Yes, unsubscribe me</button></form>""")


@router.post("/public/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db), t: str | None = None):
    """Form submit from the page above, or a mail client's one-click unsubscribe (RFC 8058)."""
    token = t
    if not token:
        form = await request.form()
        token = form.get("t")
    email = _unsubscribe(db, token or "")
    if not email:
        return _page("Link not valid", "<p>This unsubscribe link isn't valid.</p>")
    return _page("You're unsubscribed", f"<p>{html.escape(email)} won't receive any more emails from us.</p>")


# ------------------------------------------------------------------ approve from email (public, signed links)

def _approval_page(approval_id: int, action: str, r: str, e: int, s: str, db: Session):
    from ..models import Approval
    from ..services import emailapprove

    problem = emailapprove.verify(approval_id, action, r, e, s)
    a = db.get(Approval, approval_id)
    if problem or not a:
        return None, problem or "Draft not found."
    return a, None


@router.get("/public/approvals/{approval_id}/{action}")
def approval_link_page(approval_id: int, action: str, r: str, e: int, s: str, db: Session = Depends(get_db)):
    """Confirmation page. Nothing changes until the button is clicked (link scanners only ever GET)."""
    a, problem = _approval_page(approval_id, action, r, e, s, db)
    if problem:
        return _page("Link not valid", f"<p>{html.escape(problem)}</p>")
    if a.status != "pending":
        return _page("Already decided", f"<p>This draft is already <b>{html.escape(a.status)}</b>.</p>")
    who = html.escape(a.contact.full_name if a.contact else "")
    verb = "Approve and send" if action == "approve" else "Reject"
    reason = ("" if action == "approve" else
              '<p><select name="reason"><option value="too_generic">Too generic</option><option value="wrong_facts">'
              'Wrong or unverifiable facts</option><option value="tone">Tone</option><option value="too_long">Too long'
              '</option><option value="timing">Bad timing</option><option value="other">Other</option></select></p>')
    body = (f"<p><b>To:</b> {who}<br><b>Subject:</b> {html.escape(a.subject or '')}</p>"
            f"<pre style='white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:6px'>{html.escape(a.body)}</pre>"
            f"<form method='post'>{reason}<button type='submit'>{verb}</button></form>"
            "<p style='color:#777'>Want to change the wording? Open it in RevenueOS instead.</p>")
    return _page(f"{verb}?", body)


@router.post("/public/approvals/{approval_id}/{action}")
async def approval_link_submit(approval_id: int, action: str, request: Request, r: str, e: int, s: str,
                               db: Session = Depends(get_db)):
    from ..services import approvals as approvals_svc

    a, problem = _approval_page(approval_id, action, r, e, s, db)
    if problem:
        return _page("Link not valid", f"<p>{html.escape(problem)}</p>")
    reviewer = f"{r} (email link)"
    try:
        if action == "approve":
            approvals_svc.approve_and_send(db, a, reviewer)
        else:
            form = await request.form()
            approvals_svc.reject(db, a, reviewer, "Rejected from email link", form.get("reason") or "other")
        db.commit()
    except approvals_svc.ApprovalError as exc:
        db.commit()
        return _page("Couldn't do that", f"<p>{html.escape(str(exc))}</p><p>Open the draft in RevenueOS to fix it.</p>")
    done = {"sent": "Sent.", "approved": "Approved: it goes out on schedule.", "rejected": "Rejected."}.get(a.status, a.status)
    return _page("Done", f"<p>{html.escape(done)}</p>")
