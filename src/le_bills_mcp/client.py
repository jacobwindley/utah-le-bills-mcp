"""HTTP client for le.utah.gov public data and the token-gated GLEN API."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from .cache import TTLCache
from .session import candidate_default_sessions, normalize_session

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://le.utah.gov/data/"
SITE_BASE = "https://le.utah.gov/"
GLEN_BASE = "https://glen.le.utah.gov/"
USER_AGENT = "le-bills-mcp/0.1 (+https://le.utah.gov/data/developer.htm)"

# TTLs (seconds), chosen to respect the documented polling limits.
HOUR = 3600
DAY = 24 * HOUR
TTL_LEGISLATORS = DAY
TTL_COMMITTEES = HOUR
TTL_CALENDAR = HOUR
TTL_BILLLIST = HOUR
TTL_BILL = 600
TTL_DOC = HOUR
TTL_STATUTE = DAY


class NotFound(Exception):
    pass


class UpstreamError(Exception):
    pass


class RateLimited(UpstreamError):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TokenRequired(Exception):
    pass


class LeClient:
    def __init__(self, token: str | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.token = token
        self._http = http
        self.cache = TTLCache()
        self.request_count = 0
        self._default_session: str | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._http

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    async def fetch(self, url: str, ttl: float) -> bytes:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        self.request_count += 1
        log.debug("GET %s", self._redact(url))
        try:
            resp = await self.http.get(url)
        except httpx.HTTPError as exc:
            raise UpstreamError(self._redact(f"Request failed: {exc!r}")) from None
        if resp.status_code == 404:
            raise NotFound(self._redact(f"Not found: {url}"))
        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After", 30))
            except ValueError:
                retry_after = 30.0
            raise RateLimited(
                f"le.utah.gov is rate limiting requests; try again in about {int(retry_after)}s.",
                retry_after,
            )
        if resp.status_code >= 400:
            raise UpstreamError(self._redact(f"HTTP {resp.status_code} from {url}"))
        body = resp.content
        self.cache.set(url, body, ttl)
        return body

    async def public_json(self, path: str, ttl: float) -> Any:
        body = await self.fetch(PUBLIC_BASE + path, ttl)
        return json.loads(body.decode("utf-8-sig"))

    async def site(self, url_or_path: str, ttl: float = TTL_DOC) -> bytes:
        """Fetch a document linked from bill data (relative paths resolve against le.utah.gov)."""
        return await self.fetch(urljoin(SITE_BASE, url_or_path), ttl)

    async def glen(self, path: str, ttl: float) -> bytes:
        if not self.token:
            raise TokenRequired(
                "This tool needs a GLEN developer token. Set LE_UTAH_TOKEN (see .env.example)."
            )
        return await self.fetch(f"{GLEN_BASE}{quote(path, safe='/')}/{self.token}", ttl)

    # ---- convenience wrappers -------------------------------------------------

    async def resolve_session(self, session: str | None) -> str:
        normalized = normalize_session(session)
        if normalized:
            return normalized
        if self._default_session:
            return self._default_session
        for candidate in candidate_default_sessions():
            try:
                await self.billlist(candidate)
            except NotFound:
                continue
            self._default_session = candidate
            return candidate
        raise NotFound("Could not determine a default session; pass one explicitly, e.g. '2026'.")

    async def billlist(self, session: str) -> list[dict]:
        return await self.public_json(f"{session}/billlist.json", TTL_BILLLIST)

    async def bill(self, session: str, bill: str) -> dict:
        return await self.public_json(f"{session}/{bill}.json", TTL_BILL)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
