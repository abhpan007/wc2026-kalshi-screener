"""Tests for series-driven discovery and series-aware market mapping."""

from __future__ import annotations

from datetime import date

from screener.clients.kalshi import KalshiMarket
from screener.clients.kalshi_markets import (
    discover_matches,
    map_series_market,
    match_date_from_key,
    match_key,
    parse_matchup_title,
)
from screener.models import (
    AdvanceSelection,
    BttsSelection,
    CornersSelection,
    CorrectScoreSelection,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    TeamTotalSelection,
)
from tests.conftest import FakeSeriesKalshi, wc_match_events

HOME, AWAY = "Jordan", "Argentina"


def _m(sub: str, **kw) -> KalshiMarket:
    return KalshiMarket(ticker=kw.pop("ticker", "T"), yes_sub_title=sub, **kw)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def test_parse_matchup_title_variants():
    assert parse_matchup_title("Jordan vs Argentina") == ("Jordan", "Argentina")
    assert parse_matchup_title("Jordan vs Argentina: Total Goals") == ("Jordan", "Argentina")
    assert parse_matchup_title("FIFA World Cup: United States vs Mexico") == ("United States", "Mexico")
    assert parse_matchup_title("Brazil v Argentina") == ("Brazil", "Argentina")
    assert parse_matchup_title("no separator") is None


def test_match_key_and_date():
    assert match_key("KXWCTOTAL", "KXWCTOTAL-26JUN27JORARG") == "26JUN27JORARG"
    assert match_date_from_key("26JUN27JORARG") == date(2026, 6, 27)
    assert match_date_from_key("garbage") is None


# --------------------------------------------------------------------------- #
# Series-aware mapping
# --------------------------------------------------------------------------- #
def test_map_over_under():
    sel = map_series_market("over_under", Period.FULL, _m("Over 2.5 goals scored", yes_bid=60, yes_ask=64), home=HOME, away=AWAY)
    assert isinstance(sel, OverUnderSelection)
    assert sel.line == 2.5 and sel.side == "over" and sel.market_price_cents == 62


def test_map_first_half_total_sets_period():
    sel = map_series_market("over_under", Period.FIRST_HALF, _m("Over 0.5 goals scored"), home=HOME, away=AWAY)
    assert isinstance(sel, OverUnderSelection) and sel.period == Period.FIRST_HALF


def test_map_team_total_attributes_team():
    home_sel = map_series_market("team_total", Period.FULL, _m("Jordan over 1.5 goals"), home=HOME, away=AWAY)
    away_sel = map_series_market("team_total", Period.FULL, _m("Argentina over 0.5 goals"), home=HOME, away=AWAY)
    assert isinstance(home_sel, TeamTotalSelection) and home_sel.team == "home" and home_sel.line == 1.5
    assert isinstance(away_sel, TeamTotalSelection) and away_sel.team == "away" and away_sel.line == 0.5


def test_map_match_result():
    assert map_series_market("match_result", Period.FULL, _m("Jordan"), home=HOME, away=AWAY).outcome == "home"
    assert map_series_market("match_result", Period.FULL, _m("Argentina"), home=HOME, away=AWAY).outcome == "away"
    assert map_series_market("match_result", Period.FULL, _m("Tie"), home=HOME, away=AWAY).outcome == "draw"


def test_map_btts():
    sel = map_series_market("btts", Period.FULL, _m("Both Teams To Score"), home=HOME, away=AWAY)
    assert isinstance(sel, BttsSelection) and sel.outcome == "yes"


def test_map_advance():
    home_sel = map_series_market("advance", Period.FULL, _m("Jordan advances"), home=HOME, away=AWAY)
    away_sel = map_series_market("advance", Period.FULL, _m("Argentina advances"), home=HOME, away=AWAY)
    assert isinstance(home_sel, AdvanceSelection) and home_sel.team == "home"
    assert isinstance(away_sel, AdvanceSelection) and away_sel.team == "away"


def test_map_correct_score_team_attributed():
    # "Argentina wins 2-0" with away=Argentina -> away scored 2, home 0.
    away_win = map_series_market("correct_score", Period.FULL, _m("Argentina wins 2-0"), home=HOME, away=AWAY)
    assert isinstance(away_win, CorrectScoreSelection)
    assert away_win.home_score == 0 and away_win.away_score == 2
    # "Jordan wins 1-0" with home=Jordan -> home 1, away 0.
    home_win = map_series_market("correct_score", Period.FULL, _m("Jordan wins 1-0"), home=HOME, away=AWAY)
    assert home_win.home_score == 1 and home_win.away_score == 0
    # Draw is symmetric.
    draw = map_series_market("correct_score", Period.FULL, _m("Draw 1-1"), home=HOME, away=AWAY)
    assert draw.home_score == 1 and draw.away_score == 1


def test_map_corners():
    sel = map_series_market("corners", Period.FULL, _m("Over 9.5 corners"), home=HOME, away=AWAY)
    assert isinstance(sel, CornersSelection)


def test_map_returns_none_on_bad_shape():
    # over_under market with no "Over X" text.
    assert map_series_market("over_under", Period.FULL, _m("something"), home=HOME, away=AWAY) is None
    # team total naming neither team.
    assert map_series_market("team_total", Period.FULL, _m("Over 1.5 goals"), home=HOME, away=AWAY) is None


# --------------------------------------------------------------------------- #
# Discovery (series-driven, grouped by match key)
# --------------------------------------------------------------------------- #
def test_discover_groups_match_across_series():
    client = FakeSeriesKalshi(wc_match_events())
    matches = discover_matches(client)
    assert len(matches) == 1
    dm = matches[0]
    assert dm.home_name == "Jordan" and dm.away_name == "Argentina"
    assert dm.match_date == date(2026, 6, 21)
    assert dm.match_key == "26JUN21JORARG"
    # 3 game + 2 advance + 2 total + 2 team total + 1 btts + 2 score + 1 corners + 1 1H = 14
    assert len(dm.selections) == 14
    assert dm.unmapped == []
    kinds = {type(s).__name__ for s in dm.selections}
    assert {
        "MatchResultSelection", "AdvanceSelection", "OverUnderSelection",
        "TeamTotalSelection", "BttsSelection", "CorrectScoreSelection", "CornersSelection",
    } <= kinds
    # First-half total carries the 1H period.
    assert any(s.period == Period.FIRST_HALF for s in dm.selections)


def test_discover_two_matches_separate_keys():
    events = wc_match_events()
    events2 = wc_match_events(home="France", away="Iraq", hc="FRA", ac="IRQ")
    for k, v in events2.items():
        events[k] = events.get(k, []) + v
    matches = discover_matches(FakeSeriesKalshi(events))
    assert {m.home_name for m in matches} == {"Jordan", "France"}
