"""Read API keys from AWS Secrets Manager.

The secret is a JSON blob, e.g. ``{"ODDS_API_KEY": "...", "NEWS_API_KEY": "..."}``.
No secret values are ever logged. Client is injected for testing.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def load_secrets(secret_id: str, *, client: Any) -> dict[str, str]:
    """Return the secret's JSON as a dict. Empty dict (logged) on any failure —
    a missing secret must degrade (no odds key → reference lines missing), not
    crash the scheduled run."""
    try:
        resp = client.get_secret_value(SecretId=secret_id)
        data = json.loads(resp["SecretString"])
        log.info("secrets.loaded", secret_id=secret_id, keys=sorted(data.keys()))
        return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        log.warning("secrets.load_failed", secret_id=secret_id, error=str(exc))
        return {}
