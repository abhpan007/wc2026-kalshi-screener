"""Read-only team-news (lineup / injury) client.

Deliverable 5. Pulls confirmed/projected lineups and injuries for both teams of
a match and populates :class:`screener.models.TeamNews`.

GRACEFUL DEGRADATION IS THE WHOLE POINT OF THIS MODULE.
News is the softest input in the pipeline: a provider may be missing, rate
limited, slow, or simply unable to resolve a particular team/match. None of that
is allowed to abort a run. Every implementation here therefore returns
``TeamNews(known=False)`` rather than raising when it cannot resolve the news.

``known=False`` means "we could not find out", which is DISTINCT from "we know
and nobody is out" (``known=True`` with empty lists). Upstream confidence tagging
downgrades a fair value when news is unknown, and the screener only suppresses
injury-driven player-prop edges when news IS known and a relevant player is out.
Conflating the two would either hide real edges or surface fake ones, so the
distinction is preserved carefully throughout.

GUARDRAIL: this module defines read methods only. There is deliberately no
method that writes anywhere except the disk cache (via the injected HttpClient).
The source scanner in ``screener.guardrails`` enforces this across the package.

Provider status: the default wired into the pipeline is :class:`StubNewsClient`,
which always returns "unknown". :class:`HttpNewsClient` is a real-provider
skeleton (API-Football / api-sports.io shape) behind the same interface; it is
not enabled until a key is configured and the parsing is validated against real
captured responses.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ..models import TeamNews
from .http import HttpClient

log = structlog.get_logger(__name__)

# Env var holding the news provider API key. NEVER hardcode a key; if it is
# absent the HTTP client degrades to "unknown" rather than failing.
API_KEY_ENV = "SCREENER_NEWS_API_KEY"

# API-Football statuses that mean a player will not play vs. is a game-time call.
# Conservative mapping; re-validate against real captured responses.
_OUT_STATUSES = frozenset({"missing fixture", "out", "suspended", "injured"})
_DOUBTFUL_STATUSES = frozenset({"questionable", "doubtful"})


# --------------------------------------------------------------------------- #
# Interface (read-only). Swap providers by implementing this ABC.
# --------------------------------------------------------------------------- #
class NewsClient(ABC):
    """Read-only team-news source. NO write methods, ever.

    Implementations MUST NOT raise on a source failure: return
    ``TeamNews(known=False)`` so the pipeline degrades gracefully.
    """

    @abstractmethod
    def get_team_news(self, team_id: str) -> TeamNews:
        """News for one team. Returns ``TeamNews(known=False)`` if unresolved."""

    def get_match_news(self, home_team_id: str, away_team_id: str) -> tuple[TeamNews, TeamNews]:
        """News for both teams of a match, as ``(home, away)``.

        Default implementation resolves each side independently via
        :meth:`get_team_news`; a side that cannot be resolved is simply
        ``known=False`` and does not affect the other.
        """
        return self.get_team_news(home_team_id), self.get_team_news(away_team_id)


# --------------------------------------------------------------------------- #
# Stub (the default the pipeline uses until a real provider is wired)
# --------------------------------------------------------------------------- #
class StubNewsClient(NewsClient):
    """Always returns "unknown" news, cleanly and without ever raising.

    This is the shipped default per the spec: when no provider is available we
    prefer an honest "we don't know" over a failed run or a falsely-confident
    "everyone is fit". Wiring this in keeps the rest of the pipeline running and
    simply downgrades confidence where news matters.
    """

    def get_team_news(self, team_id: str) -> TeamNews:
        # Intentionally no network, no parsing, no failure modes: just unknown.
        log.debug("news.stub.unknown", team_id=team_id)
        return TeamNews(known=False)


# --------------------------------------------------------------------------- #
# HTTP skeleton (real provider behind the same interface) — optional, gated
# --------------------------------------------------------------------------- #
class _ApiFootballPlayer(BaseModel):
    """One injured/doubtful player entry in an API-Football injuries response."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    # API-Football nests under {"player": {...}, "type": "...", "reason": "..."}.
    type: Optional[str] = None  # availability bucket, e.g. "Missing Fixture"
    reason: Optional[str] = None


class HttpNewsClient(NewsClient):
    """Read-only news client skeleton built on the shared :class:`HttpClient`.

    Modeled on the API-Football / api-sports.io ``/injuries`` endpoint shape.
    It is a SKELETON: parsing is validated only against the synthetic fixtures
    in ``tests/fixtures/news_*.json`` and must be re-checked against real
    captured responses before being trusted.

    Crucially, EVERY public method catches source failures and returns
    ``TeamNews(known=False)`` instead of propagating. The api key is read from
    the environment (``SCREENER_NEWS_API_KEY``); if absent, every call degrades
    to "unknown".
    """

    def __init__(self, http: HttpClient, *, api_key: Optional[str] = None) -> None:
        self._http = http
        # Read the key lazily from env if not injected. Never hardcode a key.
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)

    def get_team_news(self, team_id: str) -> TeamNews:
        if not self._api_key:
            # No credential -> we genuinely cannot know. Degrade, don't fail.
            log.warning("news.http.no_api_key", team_id=team_id)
            return TeamNews(known=False)

        try:
            data = self._http.get_json(
                "/injuries",
                {"team": team_id},
                # api-sports passes the key as a header, set on the HttpClient's
                # default_headers by the caller; params carry the query only.
            )
            return self._parse(data)
        except Exception as exc:
            # Any source failure (5xx exhausted, 4xx, network, bad payload) maps
            # to "unknown" so a missing news source never aborts the run.
            log.warning("news.http.degraded", team_id=team_id, error=str(exc))
            return TeamNews(known=False)

    def _parse(self, data: object) -> TeamNews:
        """Map an API-Football ``/injuries`` payload into TeamNews.

        A well-formed empty response means "known and nobody flagged"
        (``known=True`` with empty lists), which is the load-bearing distinction
        from the unknown case returned on failure above.
        """
        if not isinstance(data, dict):
            raise ValueError("unexpected injuries payload shape")
        entries = data.get("response", [])
        if not isinstance(entries, list):
            raise ValueError("injuries 'response' is not a list")

        out: list[str] = []
        doubtful: list[str] = []
        for entry in entries:
            player = _ApiFootballPlayer.model_validate((entry or {}).get("player", {}))
            name = (player.name or "").strip()
            if not name:
                continue
            status = (player.type or "").strip().lower()
            if status in _DOUBTFUL_STATUSES:
                doubtful.append(name)
            else:
                # Default flagged players to "out": being listed on the injuries
                # feed at all means unavailable unless explicitly game-time.
                out.append(name)

        # We reached and understood the source -> news IS known.
        return TeamNews(known=True, players_out=out, players_doubtful=doubtful)
