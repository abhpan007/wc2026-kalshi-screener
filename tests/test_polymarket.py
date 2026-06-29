"""Tests for the read-only Polymarket client + mapping, against a REAL captured
Germany vs. Paraguay fixture (tests/fixtures/polymarket_ger_par.json)."""

from __future__ import annotations

from datetime import date

from screener.clients.polymarket import (
    PolymarketClient,
    PolymarketEvent,
    PolymarketMarket,
    WC_SERIES_SLUG,
    discover_matches,
    map_market,
)
from screener.models import (
    AdvanceSelection,
    BttsSelection,
    CorrectScoreSelection,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    TeamTotalSelection,
)
from tests.conftest import load_fixture

HOME, AWAY = "Germany", "Paraguay"


def _events() -> list[PolymarketEvent]:
    return [PolymarketEvent.model_validate(e) for e in load_fixture("polymarket_ger_par.json")]


class _FakePoly(PolymarketClient):
    def __init__(self, events):
        self._events = events

    def list_series_events(self, series_slug=WC_SERIES_SLUG, *, max_pages=20):
        return self._events


def _mkt(**kw) -> PolymarketMarket:
    return PolymarketMarket.model_validate(kw)


# --------------------------------------------------------------------------- #
# JSON-string parsing + price extraction
# --------------------------------------------------------------------------- #
def test_market_parses_json_fields_and_price():
    m = _mkt(question="...: O/U 2.5", sportsMarketType="totals",
             outcomes='["Over","Under"]', outcomePrices='["0.30","0.70"]',
             bestBid=0.29, bestAsk=0.31)
    assert m.outcomes == ["Over", "Under"]
    assert m.price_cents(0) == 30  # bid/ask midpoint
    assert m.price_cents(1) == 70  # from outcome prices


# --------------------------------------------------------------------------- #
# Mapping each supported type
# --------------------------------------------------------------------------- #
def test_map_total():
    s = map_market(_mkt(question="Germany vs. Paraguay: O/U 2.5", sportsMarketType="totals",
                        outcomes='["Over","Under"]', outcomePrices='["0.5","0.5"]'), home=HOME, away=AWAY)
    assert len(s) == 1 and isinstance(s[0], OverUnderSelection)
    assert s[0].line == 2.5 and s[0].side == "over" and s[0].period == Period.FULL


def test_map_first_half_total_period():
    s = map_market(_mkt(question="GER vs PAR: 1st Half O/U 1.5", sportsMarketType="first_half_totals",
                        outcomes='["Over","Under"]', outcomePrices='["0.4","0.6"]'), home=HOME, away=AWAY)
    assert s[0].period == Period.FIRST_HALF


def test_map_team_total_attribution():
    s = map_market(_mkt(question="Germany vs. Paraguay: Germany O/U 1.5", sportsMarketType="soccer_team_totals",
                        outcomes='["Over","Under"]', outcomePrices='["0.5","0.5"]'), home=HOME, away=AWAY)
    assert isinstance(s[0], TeamTotalSelection) and s[0].team == "home" and s[0].line == 1.5


def test_map_btts_and_moneyline_and_score():
    btts = map_market(_mkt(question="...: Both Teams to Score", sportsMarketType="both_teams_to_score",
                          outcomes='["Yes","No"]', outcomePrices='["0.6","0.4"]'), home=HOME, away=AWAY)
    assert isinstance(btts[0], BttsSelection) and btts[0].outcome == "yes"
    ml = map_market(_mkt(question="Will Germany win on 2026-06-29?", sportsMarketType="moneyline",
                        outcomes='["Yes","No"]', outcomePrices='["0.4","0.6"]'), home=HOME, away=AWAY)
    assert isinstance(ml[0], MatchResultSelection) and ml[0].outcome == "home"
    cs = map_market(_mkt(question="Exact Score: Germany 1 - 3 Paraguay?", sportsMarketType="soccer_exact_score",
                        outcomes='["Yes","No"]', outcomePrices='["0.01","0.99"]'), home=HOME, away=AWAY)
    assert isinstance(cs[0], CorrectScoreSelection) and cs[0].home_score == 1 and cs[0].away_score == 3


def test_map_advance_two_outcomes():
    s = map_market(_mkt(question="...: Team to Advance", sportsMarketType="soccer_team_to_advance",
                        outcomes='["Germany","Paraguay"]', outcomePrices='["0.645","0.355"]',
                        bestBid=0.64, bestAsk=0.65), home=HOME, away=AWAY)
    assert {x.team for x in s} == {"home", "away"}
    home_adv = next(x for x in s if x.team == "home")
    assert isinstance(home_adv, AdvanceSelection) and home_adv.kalshi_price_cents in (64, 65)


def test_map_skips_unmodeled_types():
    for t in ("spreads", "second_half_totals", "soccer_penalty_shootout", "soccer_extra_time"):
        assert map_market(_mkt(question="x", sportsMarketType=t, outcomes='["a","b"]',
                               outcomePrices='["0.5","0.5"]'), home=HOME, away=AWAY) == []


# --------------------------------------------------------------------------- #
# Discovery on the real fixture
# --------------------------------------------------------------------------- #
def test_discover_real_match():
    matches = discover_matches(_FakePoly(_events()))
    assert len(matches) == 1
    dm = matches[0]
    assert dm.home_name == "Germany" and dm.away_name == "Paraguay"
    assert dm.match_date == date(2026, 6, 29)
    kinds = {type(s).__name__ for s in dm.selections}
    assert {
        "MatchResultSelection", "OverUnderSelection", "TeamTotalSelection",
        "BttsSelection", "CorrectScoreSelection", "AdvanceSelection",
    } <= kinds
    # to-advance produced both sides, with sane prices that sum to ~100
    adv = sorted((s for s in dm.selections if isinstance(s, AdvanceSelection)), key=lambda s: s.team)
    assert len(adv) == 2
    assert 95 <= sum(s.kalshi_price_cents for s in adv) <= 105
    # spreads / 2nd-half / props were skipped -> show up as unmapped, not priced
    assert len(dm.unmapped) > 0
    # every priced selection carries a price
    assert all(s.kalshi_price_cents is not None for s in dm.selections)
