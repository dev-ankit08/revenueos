"""Which web pages the assistants really saw.

Researchers list their sources themselves, and a model can write a plausible URL it never visited.
Every URL returned by web_search / web_fetch (or cited) is recorded here when a run finishes
(agents/base.py), so briefs, dossiers and draft claims can be checked against what was actually read.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SeenSource

_TRACKING = ("utm_", "gclid", "fbclid", "mc_", "ref", "ref_src")


def normalise(url: str) -> str:
    """Comparable form: lower-case host, no scheme/www difference, no fragment, tracking params or trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(_TRACKING)))
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("", host, path, query, ""))[2:][:512]


def record(db: Session, urls, run_id: int | None = None, agent: str | None = None) -> int:
    keys = {normalise(u): u for u in urls if isinstance(u, str) and u.startswith("http")}
    if not keys:
        return 0
    have = set(db.scalars(select(SeenSource.url_key).where(SeenSource.url_key.in_(list(keys)))))
    new = [SeenSource(url_key=k, url=u, run_id=run_id, agent=agent) for k, u in keys.items() if k not in have]
    db.add_all(new)
    return len(new)


def seen(db: Session, url: str) -> bool:
    return db.scalar(select(SeenSource.id).where(SeenSource.url_key == normalise(url))) is not None


def unverified(db: Session, urls) -> list[str]:
    """The URLs in `urls` that no assistant ever found or opened."""
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
    if not urls:
        return []
    keys = {normalise(u): u for u in urls}
    have = set(db.scalars(select(SeenSource.url_key).where(SeenSource.url_key.in_(list(keys)))))
    return [u for k, u in keys.items() if k not in have]


def urls_in_text(text: str) -> list[str]:
    import re

    return re.findall(r"https?://[^\s<>()\"',]+", text or "")
