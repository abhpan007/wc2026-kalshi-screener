"""End-to-end pipeline tests with in-memory fakes (no network)."""

from __future__ import annotations

from datetime import date

from screener.config import Settings
from screener.pipeline import build_daily_report, parse_matchup
from tests.conftest import FakeOdds, FakeSeriesKalshi, fair_odds_event, wc_match_events


def _settings() -> Settings:
    return Settings()


def test_parse_matchup_reexport():
    assert parse_matchup("Jordan vs Argentina: Total Goals") == ("Jordan", "Argentina")
    assert parse_matchup("Brazil v Argentina") == ("Brazil", "Argentina")
    assert parse_matchup("no separator") is None


def test_full_pipeline_produces_report_with_edge():
    report = build_daily_report(
        date(2026, 6, 21),
        _settings(),
        kalshi=FakeSeriesKalshi(wc_match_events()),
        odds=FakeOdds([fair_odds_event()]),
    )
    assert len(report.matches) == 1
    mr = report.matches[0]
    assert mr.match.home.name == "Jordan" and mr.match.away.name == "Argentina"
    assert mr.match.match_id == "26JUN21JORARG"
    assert mr.reference.moneyline is not None       # odds matched
    assert mr.lambdas is not None                    # priced
    assert mr.kalshi_1x2["home"] == 30               # KXWCGAME Jordan last_price
    # The rich Over 2.5 (kalshi mid 62 vs fair ~52) should flag, on the NO side.
    assert report.total_flagged >= 1
    ou = next(
        e for e in mr.screen.flagged
        if "over 2.5" in e.rationale.lower()
    )
    assert ou.side.value == "no"


def test_pipeline_degrades_without_odds():
    report = build_daily_report(
        date(2026, 6, 21),
        _settings(),
        kalshi=FakeSeriesKalshi(wc_match_events()),
        odds=FakeOdds([]),
    )
    assert len(report.matches) == 1          # still discovered + listed
    assert report.matches[0].lambdas is None  # unpriceable without reference
    assert report.total_flagged == 0


def test_pipeline_filters_other_days():
    # Match is on Jun 28 (ticker date); no odds, so date comes from the ticker.
    report = build_daily_report(
        date(2026, 6, 21),
        _settings(),
        kalshi=FakeSeriesKalshi(wc_match_events(datecode="26JUN28")),
        odds=FakeOdds([]),
    )
    assert report.matches == []


def test_pipeline_matches_odds_across_name_variants():
    # Kalshi calls it "Congo DR"; the odds feed calls it "DR Congo". The country
    # alias map must still line them up so the match gets reference lines.
    kalshi = FakeSeriesKalshi(wc_match_events(home="Congo DR", away="Spain", hc="COD", ac="ESP"))
    odds = FakeOdds([fair_odds_event(home="DR Congo", away="Spain")])
    report = build_daily_report(date(2026, 6, 21), _settings(), kalshi=kalshi, odds=odds)
    assert len(report.matches) == 1
    assert report.matches[0].reference.moneyline is not None  # matched despite spelling
    assert report.matches[0].lambdas is not None


def test_pipeline_no_events_is_empty_report():
    report = build_daily_report(
        date(2026, 6, 21), _settings(),
        kalshi=FakeSeriesKalshi({}), odds=FakeOdds([]),
    )
    assert report.matches == []
