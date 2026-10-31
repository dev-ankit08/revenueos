"""Fixed pipelines that run without a rep asking (code-orchestrated, so they are cheap and predictable)."""
import random
from datetime import datetime, timezone

from ..config import settings
from ..db import session_scope
from ..models import Contact
from . import researcher, scorer, writer
from .person_researcher import get_dossier, research_person

INBOUND_INSTRUCTIONS = (
    "This person came to us (inbound). Write ONE email (steps=1) replying to their message: thank them, "
    "answer what they asked using the knowledge base, show you understand their company, and propose "
    "a concrete next step (a call, with two suggested time windows this week). Warm and fast, not salesy."
)


async def inbound_lead(contact_id: int, parent_run_id: int | None = None) -> str:
    """Speed-to-lead: score -> (A/B tier) research -> draft the reply into the approval queue."""
    score = await scorer.score_contact(contact_id, parent_run_id=parent_run_id)
    if score.disqualify or score.tier in ("C", "D"):
        return f"Scored {score.total_score} ({score.tier}); routed to nurture, no reply drafted."
    with session_scope() as db:
        account_id = db.get(Contact, contact_id).account_id
    await researcher.research_account(
        account_id, focus="Why might they have reached out now?", parent_run_id=parent_run_id
    )
    await writer.write_sequence(
        contact_id, steps=1, instructions=INBOUND_INSTRUCTIONS, parent_run_id=parent_run_id
    )
    return f"Scored {score.total_score} ({score.tier}); researched and drafted a reply for approval."


async def outbound_batch(contact_ids: list[int], steps: int = 3, parent_run_id: int | None = None) -> str:
    """Score, research and sequence a batch of outbound leads, skipping weak fits."""
    researched: set[int] = set()
    sequenced = skipped = held_out = 0
    for contact_id in contact_ids:
        score = await scorer.score_contact(contact_id, parent_run_id=parent_run_id)
        if score.disqualify or score.tier == "D":
            skipped += 1
            continue
        # Randomise among ELIGIBLE leads only, so the holdout is comparable to who we contact.
        if assign_group(contact_id) == "holdout":
            held_out += 1
            continue
        with session_scope() as db:
            account_id = db.get(Contact, contact_id).account_id
        if account_id not in researched:
            await researcher.research_account(account_id, parent_run_id=parent_run_id)
            researched.add(account_id)
        if score.tier in ("A", "B") and not get_dossier(contact_id):
            await research_person(contact_id, parent_run_id=parent_run_id)  # worth it for strong fits
        await writer.write_sequence(contact_id, steps=steps, parent_run_id=parent_run_id)
        sequenced += 1
    return (f"Sequenced {sequenced} leads across {len(researched)} accounts; skipped {skipped} weak fits; "
            f"held out {held_out} as a control group.")


def assign_group(contact_id: int) -> str:
    """Randomly assign a contact to treatment or holdout once; later batches keep the first assignment."""
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if c.experiment_group is None:
            c.experiment_group = "holdout" if random.random() < settings.holdout_pct else "treatment"
            c.experiment_assigned_at = datetime.now(timezone.utc)
        return c.experiment_group
