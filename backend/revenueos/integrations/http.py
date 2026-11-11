"""Tiny JSON-over-HTTPS client for provider APIs (Google, Microsoft Graph, Gong, Slack).

Standard library only; every external call goes through `request()`, which tests replace.
"""
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
MAX_ATTEMPTS = 4


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
    text = _send_with_retries(method, url, data, hdrs, timeout)
    if raw:
        return text
    return json.loads(text) if text.strip() else {}


def _send_with_retries(method: str, url: str, data: bytes | None, hdrs: dict, timeout: int) -> str:
    """Retry rate limits (any method: the provider did not process the request) and, for idempotent
    methods, server errors and network failures. Exponential backoff with jitter; honours Retry-After."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed provider hosts)
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or (exc.code in RETRY_STATUSES and method in IDEMPOTENT)
            if not retryable or attempt == MAX_ATTEMPTS:
                raise HTTPError(exc.code, body, url) from None
            wait = _retry_after(exc.headers.get("Retry-After")) or _backoff(attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if method not in IDEMPOTENT or attempt == MAX_ATTEMPTS:
                raise
            wait = _backoff(attempt)
            log.info("network error from %s (%s); retrying", url.split("?")[0], exc)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25)


def _retry_after(value: str | None) -> float | None:
    try:
        return min(30.0, max(0.0, float(value))) if value else None
    except ValueError:
        return None
