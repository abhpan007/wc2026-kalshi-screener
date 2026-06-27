"""Disk cache for API responses, partitioned per match-day.

Reruns for the same match-day must not hammer the upstream APIs, so every GET
is cached to ``<root>/<namespace>/<source>/<key-hash>.json``. The namespace is
normally the match-day date string; a new day gets a fresh partition, and a
``force_refresh`` flag bypasses the cache when you explicitly want live data.

JSON on disk (not pickle) so cached responses are inspectable by hand — useful
when debugging why a market was or wasn't mapped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


class DiskCache:
    """A simple keyed JSON cache scoped to one (namespace, source) partition."""

    def __init__(self, root: Path | str, namespace: str, source: str) -> None:
        self.dir = Path(root) / namespace / source
        self.namespace = namespace
        self.source = source

    def _path(self, key: str) -> Path:
        return self.dir / f"{_key_hash(key)}.json"

    def get(self, key: str) -> Optional[Any]:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt cache entry should never break a run; treat as a miss.
            log.warning("cache.read_failed", path=str(path), error=str(exc))
            return None
        log.debug("cache.hit", source=self.source, key=key)
        return data["value"]

    def set(self, key: str, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        # Store the original key alongside the value so a human browsing the
        # cache can tell what each hashed file corresponds to.
        path.write_text(
            json.dumps({"key": key, "value": value}, indent=2), encoding="utf-8"
        )
        log.debug("cache.store", source=self.source, key=key)


class NullCache(DiskCache):
    """A cache that stores nothing — used when caching is disabled."""

    def __init__(self) -> None:  # noqa: D107 - trivial
        super().__init__(root=".", namespace="_null", source="_null")

    def get(self, key: str) -> Optional[Any]:
        return None

    def set(self, key: str, value: Any) -> None:
        return None
