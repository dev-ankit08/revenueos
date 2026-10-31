"""Tiny JSON-over-HTTPS client for provider APIs (Google, Microsoft Graph, Gong, Slack).

Standard library only; every external call goes through `request()`, which tests replace.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body[:300]}")
        self.status, self.body = status, body


def request(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
            json_body: dict | None = None, form: dict | None = None, raw: bool = False, timeout: int = 60):
    """Returns parsed JSON (or text when raw=True). Raises HTTPError on non-2xx."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    data, hdrs = None, dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed provider hosts)
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise HTTPError(exc.code, exc.read().decode("utf-8", errors="replace"), url) from None
    if raw:
        return text
    return json.loads(text) if text.strip() else {}
