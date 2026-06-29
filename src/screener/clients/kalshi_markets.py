"""Discover World Cup matches from Kalshi and map markets to priceable selections.

Kalshi structures WC markets as ONE SERIES PER MARKET TYPE, with one event per
match inside each series (verified live 2026-06-21 — see the memory note
"kalshi-wc-market-structure"). For example, Jordan vs Argentina on Jun 27 has:
    KXWCGAME-26JUN27JORARG       markets: "Jordan" / "Argentina" / "Tie"
    KXWCTOTAL-26JUN27JORARG      markets: "Over 0.5 goals scored", "Over 1.5...", ...
    KXWCTEAMTOTAL-26JUN27JORARG  markets: "Jordan over 0.5 goals", ...
    KXWCBTTS-26JUN27JORARG       markets: "Both Teams To Score"
    KXWCSCORE-26JUN27JORARG      markets: "Draw 0-0", "Colombia wins 1-0", ...
    KXWCCORNERS / KXWC1HTOTAL / ...

So discovery is SERIES-DRIVEN (pull each game-market series' events, with nested
markets) and grouped by the shared ticker suffix (``26JUN27JORARG``), and mapping
is SERIES-AWARE (the series tells us the market type, which is far more robust
than guessing from free text). Unmapped markets are kept, never dropped.

Still provisional in places (team-name matching for team totals / correct score;
the team-code/date parse from the ticker) but now grounded in the real schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import structlog

from ..models import (
    AdvanceSelection,
    BttsSelection,
    CornersSelection,
    CorrectScoreSelection,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    Selection,
    TeamTotalSelection,
)
from .kalshi import KalshiEvent, KalshiMarket, MarketDataClient

log = structlog.get_logger(__name__)

# Series ticker -> (market kind, period). This is also the set discovery queries.
# Spreads (KXWCSPREAD) and 2nd-half series are omitted: no model for them yet.
GAME_MARKET_SERIES: dict[str, tuple[str, Period]] = {
    "KXWCGAME": ("match_result", Period.FULL),  # "Regulation Time" 3-way (keeps Tie)
    "KXWCADVANCE": ("advance", Period.FULL),  # knockout 2-way "to advance" (incl. ET/pens)
    "KXWCTOTAL": ("over_under", Period.FULL),
    "KXWCTEAMTOTAL": ("team_total", Period.FULL),
    "KXWCBTTS": ("btts", Period.FULL),
    "KXWCSCORE": ("correct_score", Period.FULL),
    "KXWCCORNERS": ("corners", Period.FULL),
    "KXWCTCORNERS": ("corners", Period.FULL),
    "KXWC1HTOTAL": ("over_under", Period.FIRST_HALF),
    "KXWC1HBTTS": ("btts", Period.FIRST_HALF),
    "KXWC1HSCORE": ("correct_score", Period.FIRST_HALF),
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATECODE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")
_OVER = re.compile(r"over\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_SCORE = re.compile(r"(\d+)\s*-\s*(\d+)")
# Split a matchup on " vs ", " vs. ", or " v " (Kalshi uses both forms).
_VS = re.compile(r"\s+vs?\.?\s+", re.I)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_matchup_title(title: str) -> Optional[tuple[str, str]]:
    """Extract (home, away) from a Kalshi title.

    Handles both "Jordan vs Argentina", "Jordan vs Argentina: Total Goals", and
    the futures form "FIFA World Cup: United States vs Mexico" by scanning each
    colon-delimited segment for an "X vs Y" pattern. Home is the left side.
    """
    for seg in title.split(":"):
        parts = [p.strip() for p in _VS.split(seg.strip()) if p.strip()]
        if len(parts) == 2:
            return parts[0], parts[1]
    return None


# Backwards-compatible alias (the pipeline and tests import this name).
parse_matchup = parse_matchup_title


def match_key(series_ticker: str, event_ticker: str) -> str:
    """The shared per-match suffix, e.g. KXWCTOTAL-26JUN27JORARG -> 26JUN27JORARG.
    Identical across series for the same match, so it's the grouping key."""
    prefix = f"{series_ticker}-"
    return event_ticker[len(prefix):] if event_ticker.startswith(prefix) else event_ticker


def match_date_from_key(key: str) -> Optional[date]:
    """Parse the leading date code (e.g. '26JUN27' -> 2026-06-27), or None."""
    m = _DATECODE.match(key)
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon.upper())
    if not month:
        return None
    try:
        return date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None


def _name_in(name: str, text: str) -> bool:
    return name.lower() in text


def _team_of(text: str, home: str, away: str) -> Optional[str]:
    h, a = _name_in(home, text), _name_in(away, text)
    if h and not a:
        return "home"
    if a and not h:
        return "away"
    return None


# --------------------------------------------------------------------------- #
# Series-aware mapping
# --------------------------------------------------------------------------- #
def map_series_market(
    kind: str, period: Period, market: KalshiMarket, *, home: str, away: str
) -> Optional[Selection]:
    """Map one market to a Selection given its series KIND. Returns None if the
    market's label doesn't fit the expected shape (kept as 'unmapped')."""
    text = (market.yes_sub_title or market.subtitle or "").lower()
    price = market.yes_price_cents()

    def attach(sel: Selection) -> Selection:
        sel.market_id = market.ticker
        sel.market_price_cents = price
        sel.period = period
        return sel

    if kind == "corners":
        return attach(CornersSelection(description=market.yes_sub_title or market.ticker))

    if kind == "over_under":
        m = _OVER.search(text)
        return attach(OverUnderSelection(line=float(m.group(1)), side="over")) if m else None

    if kind == "team_total":
        m = _OVER.search(text)
        team = _team_of(text, home, away)
        if not m or team is None:
            return None
        return attach(TeamTotalSelection(team=team, line=float(m.group(1)), side="over"))

    if kind == "btts":
        return attach(BttsSelection(outcome="yes"))

    if kind == "match_result":
        if "tie" in text or "draw" in text:
            return attach(MatchResultSelection(outcome="draw"))
        team = _team_of(text, home, away)
        return attach(MatchResultSelection(outcome=team)) if team else None

    if kind == "advance":
        # "Colombia advances" / "Ghana advances" -> the team that advances.
        team = _team_of(text, home, away)
        return attach(AdvanceSelection(team=team)) if team else None

    if kind == "correct_score":
        sc = _SCORE.search(text)
        if not sc:
            return None
        a, b = int(sc.group(1)), int(sc.group(2))
        # Scoreline is team-attributed in words ("Colombia wins 1-0"), NOT
        # positional, so we assign by which team the text names.
        if "draw" in text or "tie" in text:
            hs, as_ = a, b
        elif _name_in(home, text):
            hs, as_ = a, b  # home wins a-b
        elif _name_in(away, text):
            hs, as_ = b, a  # away wins a-b -> away scored a
        else:
            return None
        return attach(CorrectScoreSelection(home_score=hs, away_score=as_))

    return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@dataclass
class DiscoveredMatch:
    match_key: str
    home_name: str
    away_name: str
    match_date: Optional[date]
    selections: list[Selection] = field(default_factory=list)
    unmapped: list[KalshiMarket] = field(default_factory=list)


def discover_matches(
    client: MarketDataClient,
    *,
    series: Optional[list[str]] = None,
    statuses: tuple[str, ...] = ("open",),
) -> list[DiscoveredMatch]:
    """Pull the WC game-market series, group events by match, and map markets.

    One ``list_events`` call per (series, status), with nested markets. Events
    are grouped by their shared ticker suffix; team identity comes from any
    event's title. The HTTP client handles Kalshi's rate-limiting (429) via
    retries; querying only the game-market series keeps the call count down.
    """
    series = series if series is not None else list(GAME_MARKET_SERIES)
    grouped: dict[str, list[tuple[str, KalshiEvent]]] = {}
    seen: set[str] = set()

    for s in series:
        for status in statuses:
            try:
                events = client.list_events(series_ticker=s, status=status, with_markets=True)
            except Exception as exc:
                log.warning("discover.series_failed", series=s, status=status, error=str(exc))
                continue
            for e in events:
                if e.event_ticker in seen:
                    continue
                seen.add(e.event_ticker)
                grouped.setdefault(match_key(s, e.event_ticker), []).append((s, e))

    matches: list[DiscoveredMatch] = []
    for key, items in grouped.items():
        teams = next((t for _, e in items if e.title and (t := parse_matchup_title(e.title))), None)
        if not teams:
            log.info("discover.no_teams", key=key)
            continue
        home, away = teams
        dm = DiscoveredMatch(key, home, away, match_date_from_key(key))
        for s, e in items:
            kind, period = GAME_MARKET_SERIES.get(s, (None, Period.FULL))
            for m in e.markets:
                sel = map_series_market(kind, period, m, home=home, away=away) if kind else None
                (dm.selections if sel is not None else dm.unmapped).append(sel or m)
        matches.append(dm)
        log.info(
            "discover.match", key=key, home=home, away=away,
            mapped=len(dm.selections), unmapped=len(dm.unmapped),
        )
    return matches
