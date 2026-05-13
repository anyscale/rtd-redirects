"""Thin client over the Read the Docs v3 API.

Handles authentication, pagination, token-bucket rate limiting at 60 rpm,
and 429 retry with ``Retry-After``. Wraps the redirect CRUD endpoints used
by ``rtd-redirects`` plus the versions endpoint that ``expand.py`` calls
when resolving multi-version YAML entries.

The API token is read from ``RTD_API_TOKEN`` and never logged, printed,
or written to disk. Mutating calls log ``METHOD URL -> status`` to stderr
so a CI run produces an audit trail without exposing the token.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from collections.abc import Iterator
from typing import Any

import requests

from rtd_redirects.model import Redirect

LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://readthedocs.com/api/v3"
DEFAULT_RPM = 60
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT_SECONDS = 30


class RtdClientError(Exception):
    """Base exception for RtD client errors."""


class RtdAuthError(RtdClientError):
    """Authentication failed; check RTD_API_TOKEN."""


class RtdRateLimitError(RtdClientError):
    """Rate limit exceeded after the configured retry budget."""


class _TokenBucket:
    """Token bucket rate limiter.

    Refills at ``rpm/60`` tokens per second up to ``rpm`` capacity. ``acquire``
    blocks until one token is available, then consumes it. A small uniform
    jitter is added to each sleep so parallel runners against the same project
    don't synchronize their request times.
    """

    def __init__(self, rpm: int) -> None:
        self._rate = rpm / 60.0
        self._capacity = float(rpm)
        self._tokens = float(rpm)
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(
                self._capacity,
                self._tokens + (now - self._last_refill) * self._rate,
            )
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            deficit = 1 - self._tokens
            time.sleep(deficit / self._rate + random.uniform(0, 0.1))


class RtdClient:
    """Read the Docs v3 API client scoped to one project."""

    def __init__(
        self,
        project_slug: str,
        *,
        token: str | None = None,
        base_url: str | None = None,
        rate_limit_rpm: int = DEFAULT_RPM,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.project_slug = project_slug
        self.token = token or os.environ.get("RTD_API_TOKEN")
        if not self.token:
            raise RtdAuthError(
                "RTD_API_TOKEN is not set. Export your RtD API token before running."
            )
        self.base_url = (base_url or os.environ.get("RTD_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._bucket = _TokenBucket(rate_limit_rpm)
        self._session = session or requests.Session()
        self._session.headers.update({
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        })

    @property
    def redirects_url(self) -> str:
        return f"{self.base_url}/projects/{self.project_slug}/redirects/"

    @property
    def versions_url(self) -> str:
        return f"{self.base_url}/projects/{self.project_slug}/versions/"

    def list_redirects(self) -> list[Redirect]:
        return [_from_api(d) for d in self._paginate(self.redirects_url)]

    def list_versions(self, *, only_active: bool = True) -> list[str]:
        return [
            v["slug"]
            for v in self._paginate(self.versions_url)
            if not only_active or v.get("active", True)
        ]

    def create_redirect(self, r: Redirect) -> Redirect:
        data = self._request("POST", self.redirects_url, json=_to_api(r))
        return _from_api(data)

    def update_redirect(self, pk: int, r: Redirect) -> Redirect:
        data = self._request("PUT", f"{self.redirects_url}{pk}/", json=_to_api(r))
        return _from_api(data)

    def delete_redirect(self, pk: int) -> None:
        self._request("DELETE", f"{self.redirects_url}{pk}/")

    def _paginate(self, url: str) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            payload = self._request("GET", next_url)
            yield from payload.get("results", [])
            next_url = payload.get("next")

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            self._bucket.acquire()
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise RtdClientError(f"{method} {url} failed: {exc}") from exc

            if response.status_code == 429:
                if attempt < self.max_retries:
                    retry_after = self._parse_retry_after(response)
                    LOG.warning(
                        "Rate limited on %s %s; sleeping %.1fs (attempt %d/%d)",
                        method, url, retry_after, attempt + 1, self.max_retries,
                    )
                    time.sleep(retry_after + random.uniform(0, 1))
                    continue
                raise RtdRateLimitError(
                    f"Rate limit hit after {self.max_retries} retries on {method} {url}"
                )

            if response.status_code == 401:
                raise RtdAuthError(
                    f"Authentication failed on {method} {url}; check RTD_API_TOKEN"
                )

            if method != "GET":
                print(f"{method} {url} -> {response.status_code}", file=sys.stderr)

            if response.status_code == 204:
                return {}

            if response.status_code >= 400:
                raise RtdClientError(
                    f"{method} {url} failed with {response.status_code}: "
                    f"{response.text[:500]}"
                )

            return response.json()

        raise RtdClientError(f"{method} {url} exhausted retries unexpectedly")

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float:
        header = response.headers.get("Retry-After")
        if header is None:
            return 60.0
        try:
            return float(header)
        except ValueError:
            return 60.0

    @staticmethod
    def _backoff(attempt: int) -> None:
        base = min(2 ** attempt, 30)
        time.sleep(base + random.uniform(0, 1))


def _to_api(r: Redirect) -> dict[str, Any]:
    """Serialize a Redirect to the RtD v3 request body shape."""
    return {
        "from_url": r.from_url,
        "to_url": r.to_url,
        "type": r.type,
        "http_status": r.http_status,
        "force": r.force,
        "enabled": r.enabled,
        "position": r.position,
        "description": r.description,
    }


def _from_api(d: dict[str, Any]) -> Redirect:
    """Build a Redirect from a single RtD v3 response object."""
    return Redirect(
        from_url=d["from_url"],
        to_url=d["to_url"],
        type=d["type"],
        http_status=d.get("http_status", 301),
        force=d.get("force", False),
        enabled=d.get("enabled", True),
        position=d.get("position", 0),
        description=d.get("description") or "",
        pk=d.get("pk") or d.get("id"),
    )
