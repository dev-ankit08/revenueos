"""Load the sample knowledge base and demo leads: python -m revenueos.seed

Replace seed_data/*.md with your own offering, ICP, case studies and brand voice;
the platform has no industry-specific code.
"""
import json
from pathlib import Path

from sqlalchemy import func, select

from .api.leads import LeadIn, upsert_lead
from .db import init_db, session_scope
from .models import KBDocument
from .tools import kb

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"


def main() -> None:
    init_db()
    with session_scope() as db:
        if db.scalar(select(func.count()).select_from(KBDocument)):
            print("Knowledge base already has documents; skipping KB seed.")
        else:
            for path in sorted(SEED_DIR.glob("kb_*.md")):
                category = path.stem.split("__")[0].removeprefix("kb_")
                title = path.stem.split("__")[1].replace("_", " ").title()
                kb.add_document(db, title, category, path.read_text(encoding="utf-8"))
                print(f"KB  + [{category}] {title}")

        for lead in json.loads((SEED_DIR / "leads.json").read_text(encoding="utf-8")):
            c = upsert_lead(db, LeadIn(**lead))
            print(f"Lead + {c.full_name} @ {lead['company']}")


if __name__ == "__main__":
    main()
