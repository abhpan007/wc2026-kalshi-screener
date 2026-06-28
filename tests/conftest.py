"""Shared test fakes. No test ever touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# HTTP fakes
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json = {} if json_data is None else json_data

    def json(self) -> Any:
        return self._json


class FakeSession:
    """A requests.Session-like stub driven by a handler callable.

    ``handler(url, params) -> FakeResponse``. All calls are recorded on
    ``.calls`` so tests can assert how many requests were made.
    """

    def __init__(self, handler: Callable[[str, Optional[dict]], FakeResponse]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params=None, timeout=None, headers=None) -> FakeResponse:
        self.calls.append((url, dict(params or {})))
        return self._handler(url, params)


# --------------------------------------------------------------------------- #
# Kalshi series fakes (mirror the real one-series-per-market-type structure)
# --------------------------------------------------------------------------- #
from screener.clients.kalshi import KalshiEvent, KalshiMarket, MarketDataClient  # noqa: E402
from screener.clients.odds import (  # noqa: E402
    Bookmaker,
    OddsDataClient,
    OddsEvent,
    OddsMarket,
    OddsOutcome,
)


class FakeSeriesKalshi(MarketDataClient):
    """Returns events keyed by series ticker, like the real list_events."""

    def __init__(self, events_by_series: dict[str, list[KalshiEvent]]) -> None:
        self._m = events_by_series

    def list_events(self, *, series_ticker=None, status="open", with_markets=False):
        if status != "open":
            return []
        return list(self._m.get(series_ticker, []))

    def get_event(self, event_ticker, *, with_markets=True):
        for evs in self._m.values():
            for e in evs:
                if e.event_ticker == event_ticker:
                    return e
        raise KeyError(event_ticker)

    def list_markets(self, *, event_ticker=None, status="open"):
        return []

    def get_market(self, ticker):
        raise KeyError(ticker)


def _mk(ticker: str, sub: str, **kw) -> KalshiMarket:
    return KalshiMarket(ticker=ticker, yes_sub_title=sub, **kw)


def wc_match_events(
    *, datecode="26JUN21", home="Jordan", away="Argentina", hc="JOR", ac="ARG",
    over25_bid=60, over25_ask=64,
) -> dict[str, list[KalshiEvent]]:
    """Build one match's events across the WC game-market series, real-shaped."""
    key = f"{datecode}{hc}{ac}"

    def ev(series, title, mkts):
        return KalshiEvent(event_ticker=f"{series}-{key}", series_ticker=series, title=title, markets=mkts)

    return {
        "KXWCGAME": [ev("KXWCGAME", f"{home} vs {away}", [
            _mk(f"KXWCGAME-{key}-H", home, last_price=30),
            _mk(f"KXWCGAME-{key}-A", away, last_price=45),
            _mk(f"KXWCGAME-{key}-T", "Tie", last_price=25),
        ])],
        "KXWCADVANCE": [ev("KXWCADVANCE", f"{home} vs {away}", [
            _mk(f"KXWCADVANCE-{key}-H", f"{home} advances", last_price=42),
            _mk(f"KXWCADVANCE-{key}-A", f"{away} advances", last_price=58),
        ])],
        "KXWCTOTAL": [ev("KXWCTOTAL", f"{home} vs {away}: Total Goals", [
            _mk(f"KXWCTOTAL-{key}-1", "Over 1.5 goals scored", last_price=74),
            _mk(f"KXWCTOTAL-{key}-2", "Over 2.5 goals scored", yes_bid=over25_bid, yes_ask=over25_ask),
        ])],
        "KXWCTEAMTOTAL": [ev("KXWCTEAMTOTAL", f"{home} vs {away}: Team Total", [
            _mk(f"KXWCTEAMTOTAL-{key}-H1", f"{home} over 0.5 goals", last_price=70),
            _mk(f"KXWCTEAMTOTAL-{key}-A1", f"{away} over 1.5 goals", last_price=40),
        ])],
        "KXWCBTTS": [ev("KXWCBTTS", f"{home} vs {away}: BTTS", [
            _mk(f"KXWCBTTS-{key}-Y", "Both Teams To Score", last_price=53),
        ])],
        "KXWCSCORE": [ev("KXWCSCORE", f"{home} vs {away}: Correct Score", [
            _mk(f"KXWCSCORE-{key}-D11", "Draw 1-1", last_price=11),
            _mk(f"KXWCSCORE-{key}-A20", f"{away} wins 2-0", last_price=9),
        ])],
        "KXWCCORNERS": [ev("KXWCCORNERS", f"{home} vs {away}: Corners", [
            _mk(f"KXWCCORNERS-{key}-1", "Over 9.5 corners", last_price=58),
        ])],
        "KXWC1HTOTAL": [ev("KXWC1HTOTAL", f"{home} vs {away}: 1st Half Total", [
            _mk(f"KXWC1HTOTAL-{key}-1", "Over 0.5 goals scored", last_price=60),
        ])],
    }


class FakeOdds(OddsDataClient):
    def __init__(self, events: list[OddsEvent]) -> None:
        self._events = events

    def fetch_events(self, *, sport="soccer_fifa_world_cup"):
        return self._events

    def fetch_reference_lines(self, *, sport="soccer_fifa_world_cup"):
        from screener.clients.odds import reference_lines_from_event

        return {e.id: reference_lines_from_event(e) for e in self._events}


def fair_odds_event(
    *, home="Jordan", away="Argentina", commence="2026-06-21T22:00:00Z",
    ph=0.50, pd=0.27, pa=0.23, over=0.52,
) -> OddsEvent:
    """A vig-free odds event so de-vig reproduces the given probabilities."""
    return OddsEvent(
        id=f"odds-{home}-{away}", home_team=home, away_team=away, commence_time=commence,
        bookmakers=[Bookmaker(key="b", markets=[
            OddsMarket(key="h2h", outcomes=[
                OddsOutcome(name=home, price=1 / ph),
                OddsOutcome(name=away, price=1 / pa),
                OddsOutcome(name="Draw", price=1 / pd),
            ]),
            OddsMarket(key="totals", outcomes=[
                OddsOutcome(name="Over", price=1 / over, point=2.5),
                OddsOutcome(name="Under", price=1 / (1 - over), point=2.5),
            ]),
        ])],
    )
