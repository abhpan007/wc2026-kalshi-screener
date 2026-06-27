"""HTTP wrapper shared by all read-only clients.

Responsibilities, kept in one place so every client behaves the same:
  - GET only (this is the read-only seam; there is intentionally no post/put/etc.)
  - explicit connect/read timeouts
  - tenacity retries with exponential backoff on transient failures
  - transparent disk caching keyed by URL + params

The ``session`` is injected (anything with a ``requests.Session``-like ``.get``)
so tests can pass a stub and we never touch the network in CI.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlencode

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .cache import DiskCache, NullCache

log = structlog.get_logger(__name__)

# Default connect/read timeout in seconds. Soccer APIs are not slow; fail fast.
DEFAULT_TIMEOUT = (5.0, 15.0)


class TransientHttpError(Exception):
    """A retryable HTTP failure (5xx / 429 / network). 4xx is NOT retried."""


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _SessionLike(Protocol):
    def get(
        self, url: str, *, params: Optional[Mapping[str, Any]], timeout: Any, headers: Any
    ) -> _ResponseLike: ...


class HttpClient:
    """Thin GET-only HTTP client with retries, timeouts, and disk caching."""

    def __init__(
        self,
        base_url: str,
        *,
        session: _SessionLike,
        cache: Optional[DiskCache] = None,
        timeout: Any = DEFAULT_TIMEOUT,
        default_headers: Optional[Mapping[str, str]] = None,
        max_attempts: int = 4,
        backoff_multiplier: float = 0.5,
        backoff_max: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.cache = cache or NullCache()
        self.timeout = timeout
        self.default_headers = dict(default_headers or {})
        self.max_attempts = max_attempts
        # Backoff is configurable so tests can set it to 0 and run instantly.
        self.backoff_multiplier = backoff_multiplier
        self.backoff_max = backoff_max

    def _cache_key(self, path: str, params: Optional[Mapping[str, Any]]) -> str:
        qs = urlencode(sorted((params or {}).items()))
        return f"{path}?{qs}" if qs else path

    def get_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        force_refresh: bool = False,
    ) -> Any:
        """GET ``path`` and return parsed JSON, using the cache unless refreshed."""
        key = self._cache_key(path, params)
        if not force_refresh:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        data = self._fetch_with_retries(path, params)
        self.cache.set(key, data)
        return data

    def _fetch_with_retries(self, path: str, params: Optional[Mapping[str, Any]]) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"

        @retry(
            retry=retry_if_exception_type(TransientHttpError),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.backoff_multiplier, max=self.backoff_max),
            reraise=True,
        )
        def _do() -> Any:
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.timeout, headers=self.default_headers
                )
            except Exception as exc:  # network-level errors are transient
                log.warning("http.network_error", url=url, error=str(exc))
                raise TransientHttpError(str(exc)) from exc

            status = resp.status_code
            if status >= 500 or status == 429:
                log.warning("http.transient_status", url=url, status=status)
                raise TransientHttpError(f"status {status}")
            if status >= 400:
                # Client error — not retryable, surface it.
                raise RuntimeError(f"GET {url} failed with status {status}")
            return resp.json()

        return _do()
