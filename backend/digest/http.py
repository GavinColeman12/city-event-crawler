"""One shared HTTP client configuration for all collectors."""
from __future__ import annotations

import httpx

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-GB,en;q=0.9,es;q=0.6",
}


def make_client(**overrides) -> httpx.AsyncClient:
    """Client every collector receives.  Per-request headers may be added on top."""
    options = dict(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
    )
    options.update(overrides)
    return httpx.AsyncClient(**options)
