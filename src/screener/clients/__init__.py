"""Read-only data-source clients.

Each external source (Kalshi, odds, news) gets a thin client behind an
interface so providers can be swapped. All clients are READ-ONLY: they fetch
data and never write anywhere except the local disk cache. See ``guardrails``.

Shared building blocks live here:
  - :mod:`.cache`  — per-match-day disk cache so reruns don't hammer APIs
  - :mod:`.http`   — HTTP wrapper with timeouts + tenacity retries
"""
