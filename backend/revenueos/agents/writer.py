"""Outreach Writer (Sonnet): personalized multi-touch sequences, queued for human approval."""
import json
from typing import Literal

from anthropic import beta_async_tool
from pydantic import BaseModel, Field

from ..config import settings
from ..db import session_scope
from ..models import Activity, Contact
from ..services import approvals, memory, personalisation
from .base import new_run, run_agent
from .person_researcher import get_dossier
from .shared_tools import brief_to_dict, get_account, get_contact, kb_search

class Claim(BaseModel):
    text: str = Field(description="The factual claim as it appears in the message.")
    source: str = Field(description="kb:<knowledge-base document title>, a URL from the brief or dossier, "
                                    "or call / reply / history for what the customer told us.")


ROLE = """You are the Outreach Writer, a top-performing SDR who writes outreach that busy executives answer.

Process:
1. Call get_contact and get_account. The account's latest_brief and the PERSON DOSSIER (when given) are your
   main sources of personalization; a point the person made publicly is the strongest opener.
   If there is no brief, use what the account record says and keep personalization modest.
2. Use kb_search to pull the single most relevant proof point for this persona and industry.
3. Write the sequence and queue EVERY message with queue_message (one call per message). Nothing is sent
   until a human approves it.
4. Reply with a short summary of the sequence logic.

Writing rules:
- Follow the BRAND VOICE in the company context.
- Email 1: under 120 words. Open with something specific to them (from the brief), not about us.
  Connect it to one likely pain, offer one proof point, end with a low-friction question (not "15 minutes?").
- Follow-ups add new value each time (a different angle, insight or proof point); never "just bumping this".
- The final step is a polite break-up email.
- Subject lines: 2-6 words, lower friction, no clickbait, no ALL CAPS, no emojis.
- Make it unmistakably personal: use the RELATIONSHIP HISTORY (what they said on calls, in replies, what was
  promised) and the brief. A grader scores every draft for personalisation and sends generic ones back.
- Never reveal or closely paraphrase INTERNAL TEAM NOTES; use them only to choose your angle.
- List every factual claim in `claims` with its source (kb:<document title>, a URL from the brief or dossier,
  or call/reply/history). Reviewers see the sources beside the draft.
- Only use numbers and customer names that appear in the knowledge base or the brief. A guardrail flags
  any figure it cannot find in either, and the reviewer will see it.
- No placeholders: write the actual name and company. No signature block or unsubscribe line (added automatically).
- When asked for A/B variants, write variant B of step 1 with a genuinely different angle."""


def make_queue_tool(account_contact_ids: set[int], run_id: int, grounding: str, agent_name: str = "outreach_writer"):
    rewrites: set[tuple] = set()  # drafts already sent back once for being too generic

    @beta_async_tool
    async def queue_message(
        contact_id: int,
        channel: Literal["email", "linkedin_message"],
        sequence_step: int,
        send_after_days: int,
        body: str,
        rationale: str,
        subject: str | None = None,
        variant: Literal["A", "B"] = "A",
        claims: list[Claim] | None = None,
    ) -> str:
        """Queue one outreach message for human review. Nothing is sent until a rep approves it.

        Args:
            contact_id: Recipient contact id.
            channel: "email" or "linkedin_message" (LinkedIn messages are sent by the rep manually).
            sequence_step: 1 for the first touch, 2 for the first follow-up, and so on.
            send_after_days: Days after approval to send. 0 for step 1; e.g. 3, 7, 14 for follow-ups.
            body: Message body, plain text, final wording.
            rationale: One or two sentences for the reviewer: why this angle and which brief/KB facts it uses.
            subject: Email subject line (required for email).
            variant: "A" normally; "B" for an A/B alternative of the same step.
            claims: Every factual claim in the message (figures, customer results, things about them) with its source.
        """
        if contact_id not in account_contact_ids:
            return f"Error: contact {contact_id} is not part of this task."
        with session_scope() as db:
            c = db.get(Contact, contact_id)
            recipient = {"name": c.full_name, "title": c.title, "company": c.account.name}

        # Personalisation gate: one rewrite with feedback, then queue with a warning if still generic.
        g = await personalisation.grade(subject=subject, body=body, recipient=recipient, evidence=grounding,
                                        parent_run_id=run_id)
        key = (contact_id, sequence_step, variant)
        threshold = settings.personalisation_threshold
        if g.score < threshold and key not in rewrites:
            rewrites.add(key)
            return (f"NOT queued: personalisation score {g.score}/100 (needs {threshold}). {g.feedback} "
                    f"Generic phrases: {g.generic_phrases}. Unsupported claims: {g.unsupported_claims}. "
                    "Rewrite using specific facts from the brief and relationship history, then call queue_message again.")

        with session_scope() as db:
            contact = db.get(Contact, contact_id)
            approval = approvals.queue(
                db, kind=channel, contact=contact, subject=subject, body=body,
                sequence_step=sequence_step, send_after_days=send_after_days, variant=variant,
                rationale=rationale, created_by_agent=agent_name, run_id=run_id,
                grounding_extra=grounding, claims=[c.model_dump() for c in claims or []],
            )
            approval.personalisation_score = g.score
            approval.personalisation_notes = g.model_dump(exclude={"score"})
            if g.score < threshold:
                approval.guardrail_flags = [*approval.guardrail_flags, {
                    "severity": "warn", "code": "low_personalisation",
                    "message": f"Personalisation {g.score}/100 after a rewrite. {g.feedback}"}]
            flags = approval.guardrail_flags
            approval_id = approval.id
        if any(f["severity"] == "block" for f in flags):
            return f"Queued as approval {approval_id} but BLOCKED: {flags}. Fix the problem and queue a corrected version."
        if flags:
            return f"Queued as approval {approval_id} with warnings for the reviewer: {flags}"
        return f"Queued as approval {approval_id}."

    return queue_message


async def write_sequence(
    contact_id: int, steps: int = 3, instructions: str | None = None,
    ab_test: bool = False, parent_run_id: int | None = None,
) -> str:
    with session_scope() as db:
        contact = db.get(Contact, contact_id)
        if not contact:
            raise ValueError(f"No contact {contact_id}")
        if contact.experiment_group == "holdout":
            raise ValueError(f"{contact.full_name} is in the holdout control group (no AI outreach, to measure lift). "
                             "Release them from the holdout first if you really want to contact them.")
        account = contact.account
        brief = brief_to_dict(account.briefs[0] if account.briefs else None)
        grounding = str(brief or "") + (account.notes or "")
        label = f"{contact.full_name}, {contact.title or 'unknown title'} at {account.name}"

    steps = max(1, min(steps, 6))
    prompt = (
        f"Write a {steps}-step outbound sequence for contact_id={contact_id} ({label}), "
        f"account_id={account.id}. Use email for every step unless told otherwise."
    )
    if ab_test:
        prompt += " Also queue a variant B of step 1."
    if instructions:
        prompt += f"\nAdditional instructions from the rep: {instructions}"
    history = memory.context_block(account.id, contact_id)
    if history:
        prompt += "\n\n" + history
        # Figures the customer gave us (calls, replies) count as verified; internal notes never do.
        grounding += memory.context_block(account.id, contact_id, include_internal=False)
    if dossier := get_dossier(contact_id):
        prompt += ("\n\nPERSON DOSSIER (their own public words; the best material for the opener):\n"
                   + json.dumps(dossier, default=str))
        grounding += json.dumps(dossier, default=str)

    run_id = new_run("outreach_writer", settings.model_worker, prompt, parent_run_id)
    result = await run_agent(
        agent="outreach_writer",
        model=settings.model_worker,
        role_prompt=ROLE,
        prompt=prompt,
        tools=[get_contact, get_account, kb_search, make_queue_tool({contact_id}, run_id, grounding)],
        effort="medium",
        run_id=run_id,
    )
    with session_scope() as db:
        contact = db.get(Contact, contact_id)
        if contact.status in ("new", "researched"):
            contact.status = "sequenced"
        db.add(Activity(contact_id=contact_id, type="sequence_drafted", detail=f"{steps} steps queued for approval"))
    return result.text
