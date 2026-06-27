"""The Odds API (v4) read-only client + multiplicative de-vigging.

Pulls soccer / World Cup odds from The Odds API (https://the-odds-api.com) and
turns the raw per-book decimal odds into de-vigged *reference lines* the pricing
engine can anchor to. It is strictly READ-ONLY: it issues GET requests only and
has no method that writes anywhere except the shared disk cache.

Endpoint used (all GET, all read-only):
    GET /v4/sports/{sport}/odds
        ?apiKey=...&regions=us,uk,eu&markets=h2h,totals&oddsFormat=decimal

For soccer, ``h2h`` is THREE-way (Home / Draw / Away). ``totals`` gives
Over/Under at a point line. Team totals are not in the standard market set; we
pull them opportunistically (``markets=...,team_totals``) and degrade gracefully
when a book or the whole feed omits them.

The API key is a *query param* (``apiKey``), read from ``SCREENER_ODDS_API_KEY``
(Secrets Manager later). It is NEVER hardcoded and never logged. Tests inject a
fake session and fixtures, so no test needs a real key or hits the network.

De-vigging is multiplicative (proportional); see the pure functions below. Each
is hand-checkable and unit-tested independently of any HTTP.

GUARDRAIL: read methods only. No order/trade/portfolio-write identifiers appear
here; the ``screener.guardrails`` scanner enforces that across the package.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections import Counter
from statistics import median
from typing import Mapping, Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ..models import MoneylineProbs, ReferenceLines, TeamTotalLine
from .http import HttpClient

log = structlog.get_logger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
WORLD_CUP_SPORT_KEY = "soccer_fifa_world_cup"
API_KEY_ENV = "SCREENER_ODDS_API_KEY"

# Default request shaping. ``team_totals`` is requested but treated as optional:
# many books/regions don't return it, which is fine.
DEFAULT_REGIONS = "us,uk,eu"
DEFAULT_MARKETS = "h2h,totals"

# Market keys as they appear in The Odds API v4 responses.
_MARKET_H2H = "h2h"
_MARKET_TOTALS = "totals"
_MARKET_TEAM_TOTALS = "team_totals"

# Outcome labels The Odds API uses for two-way totals.
_OVER = "Over"
_UNDER = "Under"
_DRAW = "Draw"


# --------------------------------------------------------------------------- #
# Response models (extra fields ignored so schema drift doesn't break a run)
# --------------------------------------------------------------------------- #
class OddsOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    price: float  # decimal odds (oddsFormat=decimal)
    point: Optional[float] = None  # set for totals / team_totals lines
    # team_totals carries the team in ``description`` on most feeds.
    description: Optional[str] = None


class OddsMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    outcomes: list[OddsOutcome] = Field(default_factory=list)


class Bookmaker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    title: Optional[str] = None
    markets: list[OddsMarket] = Field(default_factory=list)

    def market(self, key: str) -> Optional[OddsMarket]:
        for m in self.markets:
            if m.key == key:
                return m
        return None


class OddsEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    sport_key: Optional[str] = None
    commence_time: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    bookmakers: list[Bookmaker] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure de-vigging functions (hand-checkable; unit-tested in isolation)
# --------------------------------------------------------------------------- #
def implied_prob(decimal_odds: float) -> float:
    """Implied probability of a single decimal-odds quote: ``1 / odds``.

    This includes the book's margin (the 'vig'); it is *not* yet normalized.
    """
    if decimal_odds <= 0:
        raise ValueError(f"decimal odds must be positive, got {decimal_odds}")
    return 1.0 / decimal_odds


def normalize(probs: list[float]) -> list[float]:
    """Multiplicative (proportional) de-vig: divide each prob by their sum.

    The raw implied probs of a complete market sum to > 1 by the vig; scaling
    them to sum to exactly 1 removes the margin proportionally. This is the
    'proportional / multiplicative' method (as opposed to additive/Shin).
    """
    total = sum(probs)
    if total <= 0:
        raise ValueError("cannot normalize a non-positive probability total")
    return [p / total for p in probs]


def devig_three_way(home_odds: float, draw_odds: float, away_odds: float) -> tuple[float, float, float]:
    """De-vig a single book's 1X2 (home/draw/away) decimal odds to probs summing to 1."""
    h, d, a = normalize(
        [implied_prob(home_odds), implied_prob(draw_odds), implied_prob(away_odds)]
    )
    return h, d, a


def devig_two_way(over_odds: float, under_odds: float) -> tuple[float, float]:
    """De-vig a single book's Over/Under decimal odds to (over, under) summing to 1."""
    over, under = normalize([implied_prob(over_odds), implied_prob(under_odds)])
    return over, under


def consensus_moneyline(per_book_probs: list[tuple[float, float, float]]) -> MoneylineProbs:
    """Combine per-book de-vigged 1X2 probs into one consensus.

    Method: take the **median per outcome** across books (robust to one book's
    outlier), then **re-normalize** because three independent medians need not
    sum to 1. (``MoneylineProbs`` validates sum-to-1, so re-normalization is
    mandatory, not cosmetic.) The median of a single book is that book's value.
    """
    if not per_book_probs:
        raise ValueError("need at least one book to form a consensus moneyline")
    med_home = median(p[0] for p in per_book_probs)
    med_draw = median(p[1] for p in per_book_probs)
    med_away = median(p[2] for p in per_book_probs)
    home, draw, away = normalize([med_home, med_draw, med_away])
    return MoneylineProbs(home=home, draw=draw, away=away)


def consensus_line(lines: list[float]) -> float:
    """Pick the consensus point line across books.

    Rule: the most common line (mode); ties broken by the statistical median of
    the lines. Books usually agree on the main total (e.g. 2.5); when they split
    across adjacent half-lines, the mode is the line most books actually price,
    and the median is a sensible tiebreaker that lands on a real offered line for
    an odd count.
    """
    if not lines:
        raise ValueError("need at least one line to form a consensus")
    counts = Counter(lines)
    top = max(counts.values())
    modes = [ln for ln, c in counts.items() if c == top]
    if len(modes) == 1:
        return modes[0]
    return median(lines)


def consensus_over_prob(per_book_over: list[float]) -> float:
    """Median de-vigged over-probability across books (single book = its value)."""
    if not per_book_over:
        raise ValueError("need at least one book to form a consensus over prob")
    return median(per_book_over)


# --------------------------------------------------------------------------- #
# Per-event aggregation (pure; operates on parsed models, no HTTP)
# --------------------------------------------------------------------------- #
def _moneyline_from_event(event: OddsEvent) -> tuple[Optional[MoneylineProbs], int]:
    """De-vig every book's 1X2 for one event, then form the consensus.

    A book contributes only if it quotes all three of home/draw/away. The home
    and away outcomes are matched by team name against the event; the third is
    the Draw. Returns ``(consensus, num_contributing_books)``; ``(None, 0)`` if
    no book had a complete 1X2.
    """
    home_name = event.home_team
    away_name = event.away_team
    per_book: list[tuple[float, float, float]] = []
    for bm in event.bookmakers:
        market = bm.market(_MARKET_H2H)
        if market is None:
            continue
        by_name = {o.name: o.price for o in market.outcomes}
        draw = by_name.get(_DRAW)
        home = by_name.get(home_name) if home_name else None
        away = by_name.get(away_name) if away_name else None
        if home is None or away is None or draw is None:
            # Incomplete 1X2 (or names didn't match) — skip this book, keep going.
            log.debug("odds.h2h_incomplete", book=bm.key, event_id=event.id)
            continue
        per_book.append(devig_three_way(home, draw, away))
    if not per_book:
        return None, 0
    return consensus_moneyline(per_book), len(per_book)


def _totals_from_event(event: OddsEvent) -> tuple[Optional[float], Optional[float], int]:
    """Consensus total line + de-vigged over prob at that line.

    Step 1: per book, de-vig each Over/Under *pair at the same point*. Step 2:
    pick the consensus line across books. Step 3: median the over-probabilities
    of the books that actually quote that consensus line.

    Returns ``(total_line, over_prob, num_books)``; ``(None, None, 0)`` if no
    book offered a complete two-way total.
    """
    # book -> {point: over_prob} after de-vigging each pair at that point.
    per_book_over_by_line: list[dict[float, float]] = []
    for bm in event.bookmakers:
        market = bm.market(_MARKET_TOTALS)
        if market is None:
            continue
        overs = {o.point: o.price for o in market.outcomes if o.name == _OVER and o.point is not None}
        unders = {o.point: o.price for o in market.outcomes if o.name == _UNDER and o.point is not None}
        book_lines: dict[float, float] = {}
        for point, over_odds in overs.items():
            under_odds = unders.get(point)
            if under_odds is None:
                continue  # half a pair is unusable; skip this point.
            over_prob, _ = devig_two_way(over_odds, under_odds)
            book_lines[point] = over_prob
        if book_lines:
            per_book_over_by_line.append(book_lines)
    if not per_book_over_by_line:
        return None, None, 0

    # Each book's primary line = the one it offers (if several, its median line).
    book_primary_lines = [consensus_line(list(b.keys())) for b in per_book_over_by_line]
    line = consensus_line(book_primary_lines)

    # Over probs from books that actually quote the consensus line.
    over_probs = [b[line] for b in per_book_over_by_line if line in b]
    if not over_probs:
        return None, None, 0
    return line, consensus_over_prob(over_probs), len(over_probs)


def _team_totals_from_event(event: OddsEvent) -> list[TeamTotalLine]:
    """De-vigged per-team total lines, when any book offers team totals.

    Team totals are optional and feed-dependent. The Odds API typically carries
    the team in each outcome's ``description``; we group by (team, point), de-vig
    each Over/Under pair, then take the consensus line + median over prob per
    team. Returns an empty list if no book offered team totals.
    """
    home_name = event.home_team
    away_name = event.away_team
    if not home_name and not away_name:
        return []

    # team_side -> list of per-book {point: over_prob}
    per_team: dict[str, list[dict[float, float]]] = {"home": [], "away": []}
    for bm in event.bookmakers:
        market = bm.market(_MARKET_TEAM_TOTALS)
        if market is None:
            continue
        # group this book's outcomes by (team_desc, point)
        grouped: dict[tuple[str, float], dict[str, float]] = {}
        for o in market.outcomes:
            if o.point is None or o.description is None:
                continue
            grouped.setdefault((o.description, o.point), {})[o.name] = o.price
        book_by_side: dict[str, dict[float, float]] = {"home": {}, "away": {}}
        for (team_desc, point), pair in grouped.items():
            over_odds = pair.get(_OVER)
            under_odds = pair.get(_UNDER)
            if over_odds is None or under_odds is None:
                continue
            if team_desc == home_name:
                side = "home"
            elif team_desc == away_name:
                side = "away"
            else:
                continue  # description didn't match either team
            over_prob, _ = devig_two_way(over_odds, under_odds)
            book_by_side[side][point] = over_prob
        for side in ("home", "away"):
            if book_by_side[side]:
                per_team[side].append(book_by_side[side])

    out: list[TeamTotalLine] = []
    for side in ("home", "away"):
        books = per_team[side]
        if not books:
            continue
        book_primary = [consensus_line(list(b.keys())) for b in books]
        line = consensus_line(book_primary)
        over_probs = [b[line] for b in books if line in b]
        if not over_probs:
            continue
        out.append(
            TeamTotalLine(
                team=side,  # type: ignore[arg-type]
                line=line,
                over_prob=consensus_over_prob(over_probs),
                num_books=len(over_probs),
            )
        )
    return out


def reference_lines_from_event(event: OddsEvent) -> ReferenceLines:
    """Build a :class:`ReferenceLines` from one parsed Odds API event.

    ``num_books`` is the count of distinct books that contributed to the moneyline
    (the primary signal), falling back to the totals book count if there is no
    moneyline. This feeds the upstream confidence tag.
    """
    moneyline, ml_books = _moneyline_from_event(event)
    total_line, over_prob, totals_books = _totals_from_event(event)
    team_totals = _team_totals_from_event(event)

    # num_books = books contributing to the primary (moneyline) signal; fall back
    # to the totals contributor count when there is no moneyline.
    num_books = ml_books if moneyline is not None else totals_books

    return ReferenceLines(
        moneyline=moneyline,
        total_line=total_line,
        over_prob=over_prob,
        num_books=num_books,
        team_total_lines=team_totals,
    )


# --------------------------------------------------------------------------- #
# Interface (read-only). Swap providers by implementing this ABC.
# --------------------------------------------------------------------------- #
class OddsDataClient(ABC):
    """Read-only odds data source. NO write/order methods, ever."""

    @abstractmethod
    def fetch_reference_lines(
        self, *, sport: str = WORLD_CUP_SPORT_KEY
    ) -> dict[str, ReferenceLines]:
        """Return ``{event_id: ReferenceLines}`` for every event in the sport."""
        ...

    @abstractmethod
    def fetch_events(self, *, sport: str = WORLD_CUP_SPORT_KEY) -> list[OddsEvent]:
        """Return the raw parsed events (read)."""
        ...


# --------------------------------------------------------------------------- #
# Concrete HTTP client (The Odds API v4)
# --------------------------------------------------------------------------- #
class TheOddsApiClient(OddsDataClient):
    """Concrete read-only client for The Odds API, built on the shared HttpClient.

    The API key is taken from ``api_key`` if given, else from the
    ``SCREENER_ODDS_API_KEY`` env var. It is passed as the ``apiKey`` query
    param (never a header, never logged).
    """

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: Optional[str] = None,
        regions: str = DEFAULT_REGIONS,
        markets: str = DEFAULT_MARKETS,
    ) -> None:
        self._http = http
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        self._regions = regions
        self._markets = markets

    def _params(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError(
                f"No Odds API key: set {API_KEY_ENV} or pass api_key=. "
                "The key is a query param; it is never hardcoded."
            )
        return {
            "apiKey": self._api_key,
            "regions": self._regions,
            "markets": self._markets,
            "oddsFormat": "decimal",
        }

    def fetch_events(self, *, sport: str = WORLD_CUP_SPORT_KEY) -> list[OddsEvent]:
        path = f"/sports/{sport}/odds"
        data = self._http.get_json(path, self._params())
        if not isinstance(data, list):
            # The Odds API returns a bare JSON array of events; anything else is
            # unexpected (e.g. an error object). Don't abort the whole run.
            log.warning("odds.unexpected_payload", sport=sport, type_=type(data).__name__)
            return []
        events: list[OddsEvent] = []
        for raw in data:
            try:
                events.append(OddsEvent.model_validate(raw))
            except Exception as exc:  # one malformed event shouldn't kill the rest
                log.warning("odds.event_parse_failed", sport=sport, error=str(exc))
        return events

    def fetch_reference_lines(
        self, *, sport: str = WORLD_CUP_SPORT_KEY
    ) -> dict[str, ReferenceLines]:
        out: dict[str, ReferenceLines] = {}
        for event in self.fetch_events(sport=sport):
            try:
                out[event.id] = reference_lines_from_event(event)
            except Exception as exc:
                # Mark missing for this event but keep the run going.
                log.warning("odds.reference_failed", event_id=event.id, error=str(exc))
                out[event.id] = ReferenceLines()
        return out
