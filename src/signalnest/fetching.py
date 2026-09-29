"""Synchronous HTTP client setup; no collection loop, retries, or automatic requests."""

import httpx

from signalnest.config import HttpSettings


def make_client(settings: HttpSettings) -> httpx.Client:
    """Caller owns the context manager; future orchestration enforces request intervals.

    TLS verification remains enabled. Redirects must be inspected before following;
    shell proxy/credential settings are not implicitly adopted. No network call here.
    """
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.read_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        headers={"User-Agent": settings.user_agent},
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )
