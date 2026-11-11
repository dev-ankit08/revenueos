"""Third-party data providers behind one interface each, chosen per workspace.

Enrichment (ENRICHMENT_PROVIDER): fills missing title, LinkedIn URL, industry, headcount, country and domain
from a data provider instead of the AI's memory. Only empty fields are filled; a rep's data is never overwritten.
  - apollo: Apollo.io People Enrichment (APOLLO_API_KEY)

Email verification (VERIFICATION_PROVIDER): checks an address before the first email to it, so bounces
never happen in the first place.
  - zerobounce (ZEROBOUNCE_API_KEY), neverbounce (NEVERBOUNCE_API_KEY)

All HTTP goes through integrations.http.request (retries; replaced in tests).
"""
from dataclasses import dataclass, field

from ..config import settings
from ..integrations import http


# ------------------------------------------------------------------ enrichment

@dataclass
class Enriched:
    title: str | None = None
    linkedin_url: str | None = None
    timezone: str | None = None
    company_domain: str | None = None
    industry: str | None = None
    employees: int | None = None
    country: str | None = None
    raw: dict = field(default_factory=dict)


def enrichment_provider() -> str | None:
    p = (settings.enrichment_provider or "").lower()
    if p == "apollo" and settings.apollo_api_key:
        return "apollo"
    return None


def enrich_person(*, email: str | None, first_name: str, last_name: str, company: str | None,
                  domain: str | None) -> Enriched | None:
    if enrichment_provider() == "apollo":
        return _apollo(email, first_name, last_name, company, domain)
    return None


def _apollo(email, first_name, last_name, company, domain) -> Enriched | None:
    body = {k: v for k, v in {"email": email, "first_name": first_name, "last_name": last_name,
                              "organization_name": company, "domain": domain}.items() if v}
    data = http.request("POST", "https://api.apollo.io/api/v1/people/match", json_body=body,
                        headers={"X-Api-Key": settings.apollo_api_key, "Cache-Control": "no-cache"})
    person = (data or {}).get("person") or {}
    if not person:
        return None
    org = person.get("organization") or {}
    return Enriched(title=person.get("title"), linkedin_url=person.get("linkedin_url"),
                    timezone=person.get("time_zone"), company_domain=org.get("primary_domain"),
                    industry=org.get("industry"), employees=org.get("estimated_num_employees"),
                    country=org.get("country") or person.get("country"), raw={"apollo_id": person.get("id")})


# ------------------------------------------------------------------ email verification

VALID, INVALID, RISKY, UNKNOWN = "valid", "invalid", "risky", "unknown"


def verification_provider() -> str | None:
    p = (settings.verification_provider or "").lower()
    if p == "zerobounce" and settings.zerobounce_api_key:
        return "zerobounce"
    if p == "neverbounce" and settings.neverbounce_api_key:
        return "neverbounce"
    return None


def verify_email(email: str) -> tuple[str, str]:
    """(status, provider detail). status: valid | invalid | risky (catch-all, disposable...) | unknown."""
    provider = verification_provider()
    if provider == "zerobounce":
        d = http.request("GET", "https://api.zerobounce.net/v2/validate",
                         params={"api_key": settings.zerobounce_api_key, "email": email, "ip_address": ""})
        raw = (d.get("status") or "unknown").lower()
        status = {"valid": VALID, "invalid": INVALID, "spamtrap": INVALID, "abuse": INVALID, "do_not_mail": INVALID,
                  "catch-all": RISKY}.get(raw, UNKNOWN)
        return status, f"zerobounce:{raw}" + (f"/{d.get('sub_status')}" if d.get("sub_status") else "")
    if provider == "neverbounce":
        d = http.request("GET", "https://api.neverbounce.com/v4/single/check",
                         params={"key": settings.neverbounce_api_key, "email": email})
        raw = (d.get("result") or "unknown").lower()
        status = {"valid": VALID, "invalid": INVALID, "disposable": RISKY, "catchall": RISKY}.get(raw, UNKNOWN)
        return status, f"neverbounce:{raw}"
    return UNKNOWN, "no provider"
