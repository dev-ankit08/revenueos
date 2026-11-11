"""UTM tags on links to your own site, so website analytics and attribution can credit RevenueOS.

Only links to your own domains are tagged (UTM_DOMAINS plus the sender email's domain): tagging a news
article or a prospect's site would look odd and gain nothing. Links that already carry UTM tags are left alone.
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import settings

_URL = re.compile(r"https?://[^\s<>()\"']+")
_TRAILING = ".,;:!?)"


def _enabled() -> bool:
    v = settings.utm_enabled
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def own_domains() -> set[str]:
    domains = {d.strip().lower().removeprefix("www.") for d in (settings.utm_domains or "").split(",") if d.strip()}
    if settings.sender_email and "@" in settings.sender_email:
        domains.add(settings.sender_email.split("@")[-1].lower())
    return domains


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60] or "outreach"


def tag_url(url: str, *, campaign: str, medium: str, content: str | None = None, source: str = "revenueos") -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(k.lower().startswith("utm_") for k, _ in query):
        return url
    query += [("utm_source", source), ("utm_medium", _slug(medium)), ("utm_campaign", _slug(campaign))]
    if content:
        query.append(("utm_content", _slug(content)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def tag_links(text: str, *, campaign: str, medium: str, content: str | None = None) -> str:
    if not text or not _enabled():
        return text
    domains = own_domains()
    if not domains:
        return text

    def replace(m: re.Match) -> str:
        raw = m.group(0)
        url = raw.rstrip(_TRAILING)
        trail = raw[len(url):]
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        if not any(host == d or host.endswith("." + d) for d in domains):
            return raw
        return tag_url(url, campaign=campaign, medium=medium, content=content) + trail

    return _URL.sub(replace, text)
