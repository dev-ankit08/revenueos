"""Publishing approved social posts. LinkedIn (company page or member) through the Posts API.

Set LINKEDIN_ACCESS_TOKEN (scope w_organization_social for a company page, w_member_social for a person) and
LINKEDIN_AUTHOR_URN (urn:li:organization:<id> or urn:li:person:<id>). Only approved content is published,
and only when a person clicks Publish.
"""
from ..config import settings
from ..integrations import http

LINKEDIN_VERSION = "202501"


def linkedin_configured() -> bool:
    return bool(settings.linkedin_access_token and settings.linkedin_author_urn)


def publish_linkedin(text: str) -> str:
    """Returns the URL of the new post."""
    payload = {
        "author": settings.linkedin_author_urn,
        "commentary": text[:3000],
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    data = http.request("POST", "https://api.linkedin.com/rest/posts", json_body=payload, headers={
        "Authorization": f"Bearer {settings.linkedin_access_token}", "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0"})
    post_id = (data or {}).get("id") or ""
    return f"https://www.linkedin.com/feed/update/{post_id}" if post_id else "https://www.linkedin.com/feed/"
