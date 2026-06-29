"""Tests for report rendering (markdown + HTML)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from screener.models import (
    Confidence,
    FairValue,
    Match,
    MatchLambdas,
    MoneylineProbs,
    OverUnderSelection,
    PlayerPropSelection,
    ReferenceLines,
    Team,
    XgStrategy,
)
from screener.report import DailyReport, MatchReport, render_html, render_markdown
from screener.screening import screen_match

UTC = ZoneInfo("UTC")


def _match() -> Match:
    return Match(
        match_id="m1",
        home=Team(team_id="usa", name="United States"),
        away=Team(team_id="mex", name="Mexico"),
        kickoff_utc=datetime(2026, 6, 21, 22, 0, tzinfo=UTC),
        venue_canonical="SoFi Stadium",
    )


def _lam() -> MatchLambdas:
    return MatchLambdas(lambda_home=1.69, lambda_away=1.07, strategy=XgStrategy.BOOK_ANCHORED)


def _edge_fv() -> FairValue:
    sel = OverUnderSelection(line=2.5, side="over")
    sel.market_price_cents = 62
    return FairValue(
        selection=sel, priced=True, probability=0.52, fair_price_cents=52,
        lambdas_used=_lam(), confidence=Confidence.HIGH,
    )


def _report(matches) -> DailyReport:
    return DailyReport(
        report_date=date(2026, 6, 21),
        strategy=XgStrategy.BOOK_ANCHORED,
        threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=matches,
    )


def test_markdown_has_edge_and_disclaimer():
    screen = screen_match([_edge_fv()], threshold_cents=3)
    mr = MatchReport(
        match=_match(),
        reference=ReferenceLines(moneyline=MoneylineProbs(home=0.5, draw=0.27, away=0.23), total_line=2.5, over_prob=0.52, num_books=3),
        home_news_known=True, away_news_known=True, lambdas=_lam(), screen=screen,
        market_1x2={"home": 52, "draw": 26, "away": 22},
    )
    md = render_markdown(_report([mr]))
    assert "United States vs Mexico" in md
    assert "SoFi Stadium" in md
    assert "Flagged edges (1)" in md
    assert "book total: 2.5" in md
    assert "NEVER places, sizes, or executes a trade" in md
    assert "book_anchored" in md


def test_markdown_thin_board_message():
    sel = OverUnderSelection(line=2.5, side="over")
    sel.market_price_cents = 51
    fv = FairValue(selection=sel, priced=True, probability=0.52, fair_price_cents=52, lambdas_used=_lam(), confidence=Confidence.HIGH)
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen_match([fv], threshold_cents=3))
    md = render_markdown(_report([mr]))
    assert "Thin board" in md


def test_markdown_no_matches():
    md = render_markdown(_report([]))
    assert "No World Cup matches discovered" in md


def test_suppressed_prop_is_surfaced():
    from screener.models import TeamNews

    prop = PlayerPropSelection(player="Christian Pulisic", description="anytime")
    prop.market_price_cents = 40
    fv = FairValue(selection=prop, priced=False, excluded=True)
    screen = screen_match([fv], home_news=TeamNews(known=True, players_out=["Christian Pulisic"]))
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen)
    md = render_markdown(_report([mr]))
    assert "Suppressed" in md
    assert "Pulisic" in md


def test_liquidity_line_reports_priced_count():
    # One priced market with no Kalshi price -> 0/1 priced.
    over = OverUnderSelection(line=2.5, side="over")
    fv = FairValue(selection=over, priced=True, probability=0.52, fair_price_cents=52, lambdas_used=_lam(), confidence=Confidence.HIGH)
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen_match([fv], threshold_cents=3))
    rpt = _report([mr])
    assert rpt.liquidity == (0, 1)
    md = render_markdown(rpt)
    assert "0/1 markets priced" in md and "no edges are possible" in md


def test_fair_value_sheet_shown_without_kalshi_prices():
    # Model priced markets with NO Kalshi price (the current real-world case).
    over = OverUnderSelection(line=2.5, side="over")  # market_price_cents stays None
    fv = FairValue(
        selection=over, priced=True, probability=0.52, fair_price_cents=52,
        lambdas_used=_lam(), confidence=Confidence.HIGH,
    )
    screen = screen_match([fv], threshold_cents=3)
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen)
    md = render_markdown(_report([mr]))
    assert "Model fair values" in md
    assert "over 2.5" in md and "52¢" in md
    assert "No flagged edges" in md  # no Kalshi price -> nothing to flag


def test_html_renders_table_and_disclaimer():
    screen = screen_match([_edge_fv()], threshold_cents=3)
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen, market_1x2={"home": 52})
    html = render_html(_report([mr]))
    assert "<table" in html
    assert "United States vs Mexico" in html
    assert "decision support" in html.lower()


def test_correlation_note_rendered():
    from screener.models import TeamTotalSelection

    over = OverUnderSelection(line=2.5, side="over"); over.market_id = "OV"; over.market_price_cents = 62
    ttu = TeamTotalSelection(team="home", line=1.5, side="under"); ttu.market_id = "TT"; ttu.market_price_cents = 50
    fvs = [
        FairValue(selection=over, priced=True, probability=0.52, fair_price_cents=52, lambdas_used=_lam(), confidence=Confidence.HIGH),
        FairValue(selection=ttu, priced=True, probability=0.58, fair_price_cents=58, lambdas_used=_lam(), confidence=Confidence.HIGH),
    ]
    screen = screen_match(fvs, threshold_cents=3)
    mr = MatchReport(match=_match(), lambdas=_lam(), screen=screen)
    md = render_markdown(_report([mr]))
    assert "Correlated" in md and "low scoring" in md
