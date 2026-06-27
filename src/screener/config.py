"""Configuration.

Read from environment variables locally (prefix ``SCREENER_``); in Lambda the
same values are populated from Secrets Manager / env by the deploy layer. No
secrets live in this file or anywhere in code.

Only knobs needed by deliverables 1 and 2 are wired now. API keys and AWS
resource names will be added with their respective clients.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

from .models import XgStrategy


class Settings(BaseModel):
    """Runtime configuration. Construct via :meth:`from_env`."""

    # --- modeling knobs (the ones that change model output) --------------- #
    xg_strategy: XgStrategy = XgStrategy.BOOK_ANCHORED
    first_half_fraction: float = Field(default=0.45, gt=0.0, lt=1.0)
    max_goals: int = Field(default=15, ge=5)

    # --- screening knobs -------------------------------------------------- #
    # Divergence threshold: flag when |kalshi - fair| >= this many cents.
    threshold_cents: int = Field(default=3, ge=1, le=100)

    # --- ops -------------------------------------------------------------- #
    timezone: str = "America/Chicago"
    cache_dir: str = ".cache"
    output_dir: str = "output"  # where daily reports + grades are persisted (S3 in Lambda)
    log_json: bool = False  # human-readable locally; set true in Lambda

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "Settings":
        e = env if env is not None else dict(os.environ)

        def get(key: str) -> Optional[str]:
            return e.get(f"SCREENER_{key}")

        data: dict[str, object] = {}
        if (v := get("XG_STRATEGY")) is not None:
            data["xg_strategy"] = XgStrategy(v)
        if (v := get("FIRST_HALF_FRACTION")) is not None:
            data["first_half_fraction"] = float(v)
        if (v := get("MAX_GOALS")) is not None:
            data["max_goals"] = int(v)
        if (v := get("THRESHOLD_CENTS")) is not None:
            data["threshold_cents"] = int(v)
        if (v := get("TIMEZONE")) is not None:
            data["timezone"] = v
        if (v := get("CACHE_DIR")) is not None:
            data["cache_dir"] = v
        if (v := get("OUTPUT_DIR")) is not None:
            data["output_dir"] = v
        if (v := get("LOG_JSON")) is not None:
            data["log_json"] = v.lower() in {"1", "true", "yes"}
        return cls(**data)
