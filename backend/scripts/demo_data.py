"""Sample drafts, a deal and a task, so RevenueOS can be tried without an Anthropic API key.

    cd backend
    python -m revenueos.seed          # sample leads and knowledge base (once)
    python scripts/demo_data.py       # sample outreach drafts, a deal and a task

What it creates (all clearly labelled "[DEMO]"):
- A 3-step sequence for Priya (Siemens): two emails and a LinkedIn message, waiting in Approvals.
  It goes through the real guardrails, so one step shows a warning for an invented figure.
- One email for Marcus (Crocs) with an unfilled placeholder, so you can see a blocked draft.
- An open deal for Siemens and a call task due today.
Run it again to add another set.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from revenueos.db import init_db, session_scope  # noqa: E402
from revenueos.models import Contact, Opportunity, Task  # noqa: E402
from revenueos.services import approvals  # noqa: E402


def contact(db, email: str) -> Contact:
    c = db.scalar(select(Contact).where(Contact.email == email))
    if not c:
        raise SystemExit("Sample leads not found. Run `python -m revenueos.seed` first.")
    return c


def main() -> None:
    init_db()
    with session_scope() as db:
        priya = contact(db, "priya.sample@example.com")
        marcus = contact(db, "marcus.demo@example.com")
        brief = ("Siemens is unifying factory data across its plants and investing in demand forecasting. "
                 "Priya spoke publicly about forecast accuracy as a board-level metric.")

        steps = [
            (1, 0, "email", "[DEMO] forecast accuracy as a board metric",
             "Hi Priya,\n\nYour point that forecast accuracy belongs on the board agenda stuck with me, especially "
             "with Siemens unifying factory data across its plants.\n\nWe helped a manufacturer improve forecast "
             "accuracy once plant data was unified. Is forecast accuracy something you're measured on this year?"),
            (2, 3, "email", "[DEMO] one idea on plant data",
             "Hi Priya,\n\nOne pattern we see: forecasting improves fastest when the first two or three plants share "
             "one data model before the rest follow. Our last client cut planning time by 47% that way.\n\n"
             "Worth comparing notes?"),  # 47% is not in the brief or knowledge base -> guardrail warning
            (3, 7, "linkedin_message", None,
             "Hi Priya, enjoyed your comments on forecast accuracy as a board metric. Would be glad to connect."),
        ]
        ids = []
        for step, days, kind, subject, body in steps:
            a = approvals.queue(db, kind=kind, contact=priya, subject=subject, body=body, sequence_step=step,
                                send_after_days=days, created_by_agent="outreach_writer", enroll=True,
                                grounding_extra=brief,
                                rationale="[DEMO] Opens with her public point on forecast accuracy, ties it to the "
                                          "plant data programme in the brief.")
            a.personalisation_score = 82 if step == 1 else 64
            ids.append(a.id)

        blocked = approvals.queue(db, kind="email", contact=marcus, subject="[DEMO] quick question",
                                  body="Hi [First Name], we help retailers fix CRM data. Open to a chat?",
                                  created_by_agent="outreach_writer",
                                  rationale="[DEMO] Deliberately left with a placeholder to show a blocked draft.")

        opp = db.scalar(select(Opportunity).where(Opportunity.account_id == priya.account_id,
                                                  Opportunity.name.like("[DEMO]%")))
        if not opp:
            opp = Opportunity(account_id=priya.account_id, primary_contact_id=priya.id,
                              name="[DEMO] Siemens forecasting pilot", stage="solution", amount=120000,
                              close_date=(datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat(),
                              health="amber", owner=priya.owner)
            db.add(opp)
        db.add(Task(type="call", title="[DEMO] Call Priya about the pilot scope", contact_id=priya.id,
                    owner=priya.owner, due_at=datetime.now(timezone.utc), created_by="demo"))
        db.flush()
        print(f"Created sequence drafts {ids} for Priya, blocked draft {blocked.id} for Marcus, "
              f"deal {opp.id} and a task due today.")
        print("Open http://localhost:3000/approvals to review them.")


if __name__ == "__main__":
    main()
