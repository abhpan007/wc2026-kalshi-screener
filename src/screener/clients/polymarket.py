"""Polymarket read-only client + market mapping.

Polymarket (the CFTC-regulated US prediction-market exchange) carries deeply
liquid per-match World Cup markets — verified live: moneyline, totals, team
totals, BTTS, to-advance, exact score, and first-half variants, with $100k–$900k
liquidity per market and tight spreads. This is the venue Kalshi failed to be.

STRUCTURE (verified live 2026-06-29): one match = several "events" grouped by a
shared ``gameId``, all under the ``soccer-fifwc`` series. The Gamma API's
``gameId`` filter is IGNORED, so we pull the series and group by gameId
client-side. Each event holds binary markets tagged with ``sportsMarketType``
(e.g. ``totals``, ``soccer_team_totals``, ``soccer_team_to_advance``).

Read-only: Gamma price/market data is public (no key needed for the screener).
An API key / funded account is only for the user to place bets on Polymarket US.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import (
    AdvanceSelection,
    BttsSelection,
    CorrectScoreSelection,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    Selection,
    TeamTotalSelection,
)
from .http import HttpClient
from .kalshi_markets import DiscoveredMatch  # reuse the neutral discovery shape

log = structlog.get_logger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
WC_SERIES_SLUG = "soccer-fifwc"
# Gamma 403s the default urllib UA; a browser UA is required for reads.
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (screener; read-only)"}

_OU = re.compile(r"o/u\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_SCORE = re.compile(r"(\d+)\s*-\s*(\d+)")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# --------------------------------------------------------------------------- #
# Response models (Gamma stores outcomes/prices as JSON strings)
# --------------------------------------------------------------------------- #
class PolymarketMarket(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    question: Optional[str] = None
    sports_type: Optional[str] = Field(default=None, alias="sportsMarketType")
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[float] = Field(default_factory=list, alias="outcomePrices")
    best_bid: Optional[float] = Field(default=None, alias="bestBid")
    best_ask: Optional[float] = Field(default=None, alias="bestAsk")
    last_price: Optional[float] = Field(default=None, alias="lastTradePrice")

    @field_validator("outcomes", "outcome_prices", mode="before")
    @classmethod
    def _parse_json_list(cls, v: Any):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                return []
        return v

    def price_cents(self, outcome_index: int) -> Optional[int]:
        """Price (cents) of a given outcome. Uses the bid/ask midpoint for the
        primary outcome (most current), else the stored outcome price."""
        if outcome_index == 0 and self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2 * 100)
        if outcome_index < len(self.outcome_prices):
            return round(float(self.outcome_prices[outcome_index]) * 100)
        if outcome_index == 0 and self.last_price is not None:
            return round(self.last_price * 100)
        return None


class PolymarketTeam(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None
    ordering: Optional[str] = None  # "home" | "away"


class PolymarketEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[str] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    game_id: Optional[int] = Field(default=None, alias="gameId")
    teams: list[PolymarketTeam] = Field(default_factory=list)
    markets: list[PolymarketMarket] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Client (read-only)
# --------------------------------------------------------------------------- #
class PolymarketClient:
    """Read-only Polymarket Gamma client. No order/portfolio methods, ever."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def list_series_events(self, series_slug: str = WC_SERIES_SLUG, *, max_pages: int = 20) -> list[PolymarketEvent]:
        """All open events in a series, following offset pagination (100/page)."""
        out: list[PolymarketEvent] = []
        for page in range(max_pages):
            data = self._http.get_json(
                "/events",
                {"series_slug": series_slug, "closed": "false", "limit": 100, "offset": page * 100},
            )
            evs = data if isinstance(data, list) else data.get("data", [])
            if not evs:
                break
            out.extend(PolymarketEvent.model_validate(e) for e in evs)
            if len(evs) < 100:
                break
        return out


def build_polymarket_client() -> PolymarketClient:
    """Read-only client against the public Gamma API (no key, browser UA)."""
    import requests

    return PolymarketClient(
        HttpClient(GAMMA_BASE_URL, session=requests.Session(), default_headers=DEFAULT_HEADERS)
    )


# --------------------------------------------------------------------------- #
# Mapping: Polymarket market -> our Selection(s)
# --------------------------------------------------------------------------- #
def _team_side(text: str, home: str, away: str) -> Optional[str]:
    h, a = home.lower() in text, away.lower() in text
    if h and not a:
        return "home"
    if a and not h:
        return "away"
    return None


def map_market(m: PolymarketMarket, *, home: str, away: str) -> list[Selection]:
    """Map one Polymarket market to zero or more Selections. Skips market types
    we don't model (spreads, 2nd-half, penalty/extra-time props, first-to-score)."""
    t = m.sports_type or ""
    q = (m.question or "")
    ql = q.lower()
    # Team attribution uses the LEG (text after the matchup prefix), since the
    # full question repeats "Home vs. Away" and would match both teams.
    leg = ql.rsplit(":", 1)[-1]

    def fit(sel: Selection, idx: int) -> Selection:
        sel.market_id = q or t
        sel.market_price_cents = m.price_cents(idx)  # venue price (field name is legacy)
        return sel

    # --- totals (full + first half) --------------------------------------- #
    if t in ("totals", "first_half_totals"):
        mo = _OU.search(ql)
        if not mo:
            return []
        period = Period.FIRST_HALF if t == "first_half_totals" else Period.FULL
        return [fit(OverUnderSelection(line=float(mo.group(1)), side="over", period=period), 0)]

    # --- team totals (full + first half) ---------------------------------- #
    if t in ("soccer_team_totals", "soccer_first_half_team_totals"):
        mo = _OU.search(ql)
        team = _team_side(leg, home, away)
        if not mo or team is None:
            return []
        period = Period.FIRST_HALF if "first_half" in t else Period.FULL
        return [fit(TeamTotalSelection(team=team, line=float(mo.group(1)), side="over", period=period), 0)]

    # --- both teams to score (full + first half) -------------------------- #
    if t in ("both_teams_to_score", "both_teams_to_score_first_half"):
        period = Period.FIRST_HALF if "first_half" in t else Period.FULL
        return [fit(BttsSelection(outcome="yes", period=period), 0)]

    # --- moneyline (90' result) ------------------------------------------- #
    if t == "moneyline":
        if "draw" in ql or "tie" in ql:
            return [fit(MatchResultSelection(outcome="draw"), 0)]
        team = _team_side(leg, home, away)
        return [fit(MatchResultSelection(outcome=team), 0)] if team else []

    # --- halftime result -------------------------------------------------- #
    if t == "soccer_halftime_result":
        if "draw" in ql or "tie" in ql:
            return [fit(MatchResultSelection(outcome="draw", period=Period.FIRST_HALF), 0)]
        team = _team_side(leg, home, away)
        return [fit(MatchResultSelection(outcome=team, period=Period.FIRST_HALF), 0)] if team else []

    # --- exact score ------------------------------------------------------ #
    if t == "soccer_exact_score":
        sc = _SCORE.search(q)  # "Germany 1 - 3 Paraguay" -> home 1, away 3
        if not sc:
            return []
        return [fit(CorrectScoreSelection(home_score=int(sc.group(1)), away_score=int(sc.group(2))), 0)]

    # --- to advance (2-way, outcomes = [teamA, teamB]) -------------------- #
    if t == "soccer_team_to_advance":
        out: list[Selection] = []
        for i, name in enumerate(m.outcomes):
            team = _team_side(name.lower(), home, away)
            if team is not None:
                out.append(fit(AdvanceSelection(team=team), i))
        return out

    # spreads / second-half / penalty / extra-time / first-to-score: skip.
    return []


# --------------------------------------------------------------------------- #
# Discovery: series -> matches grouped by gameId
# --------------------------------------------------------------------------- #
def _teams_of(events: list[PolymarketEvent]) -> tuple[Optional[str], Optional[str]]:
    for e in events:
        home = next((t.name for t in e.teams if t.ordering == "home" and t.name), None)
        away = next((t.name for t in e.teams if t.ordering == "away" and t.name), None)
        if home and away:
            return home, away
    return None, None


def _base_slug(events: list[PolymarketEvent]) -> str:
    # The shortest slug is the moneyline/base game slug (others add a suffix).
    return min((e.slug or "" for e in events if e.slug), key=len, default="")


def _date_of(slug: str) -> Optional[date]:
    mo = _DATE.search(slug or "")
    if not mo:
        return None
    try:
        return date(int(mo.group(1)), int(mo.group(2)), int(mo.group(3)))
    except ValueError:
        return None


def discover_matches(
    client: PolymarketClient, *, series_slug: str = WC_SERIES_SLUG
) -> list[DiscoveredMatch]:
    """Pull the WC series, group events by gameId, and map each match's markets."""
    events = client.list_series_events(series_slug)
    by_game: dict[int, list[PolymarketEvent]] = {}
    for e in events:
        if e.game_id is not None:
            by_game.setdefault(e.game_id, []).append(e)

    matches: list[DiscoveredMatch] = []
    for gid, evs in by_game.items():
        home, away = _teams_of(evs)
        if not home or not away:
            continue
        key = _base_slug(evs) or str(gid)
        dm = DiscoveredMatch(match_key=key, home_name=home, away_name=away, match_date=_date_of(key))
        for e in evs:
            for m in e.markets:
                sels = map_market(m, home=home, away=away)
                if sels:
                    dm.selections.extend(sels)
                else:
                    dm.unmapped.append(m)
        matches.append(dm)
        log.info("polymarket.match", game=key, home=home, away=away,
                 mapped=len(dm.selections), unmapped=len(dm.unmapped))
    return matches
