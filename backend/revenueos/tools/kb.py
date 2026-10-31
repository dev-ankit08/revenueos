"""Knowledge base: ingestion and retrieval.

Retrieval is BM25 over paragraph chunks. It needs no embedding service and works
on SQLite and Postgres; swap `search` for pgvector + an embedding model once the
KB grows past a few thousand chunks.
"""
import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KBChunk, KBDocument

CATEGORIES = (
    "offering", "case_study", "icp", "persona", "brand_voice",
    "competitor", "pricing", "objection", "playbook", "rules", "other",
)
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with we our you your".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """Split on blank lines, then pack paragraphs into ~max_chars chunks."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if current and len(current) + len(p) > max_chars:
            chunks.append(current)
            current = p
        else:
            current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks


def add_document(db: Session, title: str, category: str, content: str) -> KBDocument:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    doc = KBDocument(title=title, category=category, content=content)
    doc.chunks = [KBChunk(position=i, text=c) for i, c in enumerate(chunk_text(content))]
    db.add(doc)
    db.flush()
    return doc


def search(db: Session, query: str, k: int = 5, category: str | None = None) -> list[dict]:
    stmt = select(KBChunk, KBDocument).join(KBDocument)
    if category:
        stmt = stmt.where(KBDocument.category == category)
    rows = db.execute(stmt).all()
    if not rows:
        return []

    docs = [(chunk, doc, tokenize(f"{doc.title} {chunk.text}")) for chunk, doc in rows]
    n = len(docs)
    avgdl = sum(len(t) for *_, t in docs) / n
    df = Counter(term for *_, toks in docs for term in set(toks))
    q_terms = tokenize(query)
    k1, b = 1.5, 0.75

    scored = []
    for chunk, doc, toks in docs:
        tf = Counter(toks)
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / avgdl))
        if score > 0:
            scored.append((score, chunk, doc))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [
        {"doc_id": doc.id, "title": doc.title, "category": doc.category, "text": chunk.text}
        for _, chunk, doc in scored[:k]
    ]


def documents_by_category(db: Session, *categories: str) -> list[KBDocument]:
    return list(
        db.scalars(
            select(KBDocument).where(KBDocument.category.in_(categories)).order_by(KBDocument.id)
        )
    )


def company_context(db: Session) -> str:
    """The stable 'who we are' block placed in every agent's system prompt (and cached)."""
    sections = []
    for label, cats in (
        ("WHAT WE SELL", ("offering", "pricing")),
        ("IDEAL CUSTOMER PROFILE & PERSONAS", ("icp", "persona")),
        ("BRAND VOICE", ("brand_voice",)),
    ):
        docs = documents_by_category(db, *cats)
        if docs:
            body = "\n\n".join(f"### {d.title}\n{d.content}" for d in docs)
            sections.append(f"## {label}\n{body}")
    tone = [line.partition(":")[2].strip() for d in documents_by_category(db, "rules")
            for line in d.content.splitlines() if line.strip().upper().startswith("TONE:")]
    if tone:
        sections.append("## BRAND RULES (must follow)\n" + "\n".join(f"- {t}" for t in tone))
    playbook = documents_by_category(db, "playbook")
    if playbook:
        sections.append("## WHAT WORKS (learned from our own results; prefer these patterns)\n"
                        + "\n\n".join(d.content for d in playbook))
    proof = documents_by_category(db, "case_study")
    if proof:
        sections.append(
            "## AVAILABLE PROOF POINTS (titles; use kb_search for details)\n"
            + "\n".join(f"- {d.title}" for d in proof)
        )
    return "\n\n".join(sections) or "(Knowledge base is empty. Ask the user to add offering, ICP and case-study documents.)"
