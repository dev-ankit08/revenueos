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


_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ers", "er", "ed", "es", "ly", "s")


def stem(token: str) -> str:
    """Light suffix stripping so "forecasting", "forecasts" and "forecast" match. Numbers stay as they are."""
    if token.isdigit() or len(token) <= 4:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def terms(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text)]


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """A paragraph longer than a chunk is split at sentence boundaries instead of becoming one huge chunk."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if current and len(current) + len(sentence) + 1 > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_text(text: str, max_chars: int = 1200, overlap_chars: int = 200) -> list[str]:
    """Split on blank lines, pack paragraphs into ~max_chars chunks, and repeat a short tail of the previous
    chunk at the start of the next so a fact split across a boundary is still found whole."""
    paragraphs = [s for p in re.split(r"\n\s*\n", text) if p.strip() for s in _split_long(p.strip(), max_chars)]
    chunks, current = [], ""
    for p in paragraphs:
        if current and len(current) + len(p) > max_chars:
            chunks.append(current)
            tail = current[-overlap_chars:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = f"...{tail}\n\n{p}" if overlap_chars else p
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


def search(db: Session, query: str, k: int = 5, category: str | None = None,
           min_relative_score: float = 0.2) -> list[dict]:
    stmt = select(KBChunk, KBDocument).join(KBDocument)
    if category:
        stmt = stmt.where(KBDocument.category == category)
    rows = db.execute(stmt).all()
    if not rows:
        return []

    docs = [(chunk, doc, terms(f"{doc.title} {chunk.text}")) for chunk, doc in rows]
    n = len(docs)
    avgdl = sum(len(t) for *_, t in docs) / n
    df = Counter(term for *_, toks in docs for term in set(toks))
    q_terms = terms(query)
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
    if not scored:
        return []
    floor = scored[0][0] * min_relative_score  # drop weak matches that only share a common word
    return [
        {"doc_id": doc.id, "chunk_id": chunk.id, "title": doc.title, "category": doc.category, "text": chunk.text,
         "score": round(score, 3)}
        for score, chunk, doc in scored[:k] if score >= floor
    ]


def documents_by_category(db: Session, *categories: str) -> list[KBDocument]:
    return list(
        db.scalars(
            select(KBDocument).where(KBDocument.category.in_(categories)).order_by(KBDocument.id)
        )
    )


CONTEXT_BUDGET_CHARS = 40_000  # ~10k tokens; the rest stays reachable through kb_search


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n[...trimmed; use kb_search for the full document]"


def reindex(db: Session) -> int:
    """Re-chunk every document with the current chunker (after chunking changes)."""
    n = 0
    for doc in db.scalars(select(KBDocument)):
        doc.chunks = [KBChunk(position=i, text=c) for i, c in enumerate(chunk_text(doc.content))]
        n += 1
    return n


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
            per_doc = max(2_000, CONTEXT_BUDGET_CHARS // 4 // len(docs))
            body = "\n\n".join(f"### {d.title}\n{_cap(d.content, per_doc)}" for d in docs)
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
    return _cap("\n\n".join(sections), CONTEXT_BUDGET_CHARS) if sections else ("(Knowledge base is empty. Ask the user to add offering, ICP and case-study documents.)")
