"""Tests for shadow-mode grading: PnL, win/loss, void, calibration, CSV, round-trip."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from screener.grade import (
    brier_score,
    grade,
    grade_many,
    read_results_csv,
    render_grade_markdown,
)
from screener.models import (
    BttsSelection,
    Confidence,
    FairValue,
    Match,
    MatchLambdas,
    OverUnderSelection,
    Team,
    XgStrategy,
)
from screener.report import DailyReport, MatchReport
from screener.screening import screen_match
from screener.settle import MatchResultInput
from screener.storage import LocalReportStore

LAM = MatchLambdas(lambda_home=1.69, lambda_away=1.07, strategy=XgStrategy.BOOK_ANCHORED)


def _match(mid="m1") -> Match:
    return Match(
        match_id=mid,
        home=Team(team_id="usa", name="United States"),
        away=Team(team_id="mex", name="Mexico"),
        kickoff_utc=datetime(2026, 6, 21, 22, 0, tzinfo=ZoneInfo("UTC")),
    )


def _fv(sel, fair, kalshi, conf=Confidence.HIGH):
    sel.kalshi_price_cents = kalshi
    return FairValue(
        selection=sel, priced=True, probability=fair / 100, fair_price_cents=fair,
        lambdas_used=LAM, confidence=conf,
    )


def _report() -> DailyReport:
    over = OverUnderSelection(line=2.5, side="over"); over.market_id = "OU"
    btts = BttsSelection(outcome="yes")  # below threshold, for calibration only
    screen = screen_match([_fv(over, 52, 62), _fv(btts, 53, 52)], threshold_cents=3)
    return DailyReport(
        report_date=date(2026, 6, 21),
        strategy=XgStrategy.BOOK_ANCHORED, threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=[MatchReport(match=_match(), lambdas=LAM, screen=screen)],
    )


def test_no_bet_wins_when_under_hits():
    # Over 2.5 priced rich (62) vs fair 52 -> recommended side NO (entry 38).
    # Actual 1-0 (total 1) -> over is false -> NO wins -> pnl = 100-38 = +62.
    results = {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)}
    g = grade(_report(), results)
    edge = g.edges[0]
    assert edge.side.value == "no" and edge.entry_cents == 38
    assert edge.result == "win" and edge.pnl_cents == 62
    assert g.total_pnl_cents == 62 and g.wins == 1


def test_no_bet_loses_when_over_hits():
    results = {"m1": MatchResultInput(match_id="m1", home_score=2, away_score=2)}  # total 4
    g = grade(_report(), results)
    edge = g.edges[0]
    assert edge.result == "loss" and edge.pnl_cents == -38


def test_missing_result_is_ungradeable():
    g = grade(_report(), {})
    assert g.edges[0].result == "ungradeable"
    assert g.total_pnl_cents == 0 and g.graded == []


def test_void_on_integer_push():
    over = OverUnderSelection(line=3.0, side="over"); over.market_id = "OU3"
    screen = screen_match([_fv(over, 40, 50)], threshold_cents=3)
    rpt = DailyReport(
        report_date=date(2026, 6, 21), strategy=XgStrategy.BOOK_ANCHORED, threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=[MatchReport(match=_match(), lambdas=LAM, screen=screen)],
    )
    results = {"m1": MatchResultInput(match_id="m1", home_score=2, away_score=1)}  # total 3 == line
    g = grade(rpt, results)
    assert g.edges[0].result == "void" and g.edges[0].pnl_cents == 0


def test_calibration_buckets_use_all_priced_markets():
    results = {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)}
    g = grade(_report(), results)
    # over (52% fair) and btts-yes (53% fair) both land in the 50-60 bucket; both
    # resolve false for a 1-0 (no over 2.5, no BTTS), so actual rate 0%.
    bucket = next(b for b in g.calibration if b.lo == 50)
    assert bucket.n == 2
    assert bucket.actual_rate_pct == 0.0
    assert 50 <= bucket.mean_predicted_pct <= 60


def test_calibration_includes_model_priced_markets_without_kalshi_price():
    # Model priced Over 2.5 at 60% but Kalshi has no price -> NO_PRICE status.
    # Calibration must still include it (it validates the MODEL, not the market).
    over = OverUnderSelection(line=2.5, side="over")
    screen = screen_match([_fv(over, 60, None)], threshold_cents=3)
    rpt = DailyReport(
        report_date=date(2026, 6, 21), strategy=XgStrategy.BOOK_ANCHORED, threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=[MatchReport(match=_match(), lambdas=LAM, screen=screen)],
    )
    g = grade(rpt, {"m1": MatchResultInput(match_id="m1", home_score=3, away_score=0)})
    assert g.edges == []  # no Kalshi price -> nothing flagged to bet
    bucket = next(b for b in g.calibration if b.lo == 60)
    assert bucket.n == 1 and bucket.actual_rate_pct == 100.0  # total 3 -> over 2.5 hit


def test_brier_score_basics():
    assert brier_score([(1.0, True), (0.0, False)]) == 0.0       # perfect
    assert brier_score([(0.5, True), (0.5, False)]) == 0.25      # coin flip
    assert brier_score([]) is None


def test_grade_carries_brier_and_count():
    g = grade(_report(), {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)})
    assert g.n_graded_markets == 2  # over 2.5 + btts yes
    assert g.brier is not None and 0.0 <= g.brier <= 1.0


def test_grade_many_aggregates_days():
    r = _report()
    res = {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)}
    g1 = grade(r, res)
    agg = grade_many([(r, res), (r, res)])
    assert agg.n_graded_markets == 2 * g1.n_graded_markets
    assert agg.brier == g1.brier  # identical data twice -> same mean
    assert "days" in agg.label


def test_render_markdown_has_pnl_and_calibration():
    g = grade(_report(), {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)})
    md = render_grade_markdown(g)
    assert "Hypothetical PnL" in md and "Calibration" in md


def test_read_results_csv(tmp_path: Path):
    p = tmp_path / "r.csv"
    p.write_text(
        "match_id,home_score,away_score,ht_home,ht_away\n"
        "m1,2,1,1,0\n"
        "m2,0,0,,\n",
        encoding="utf-8",
    )
    out = read_results_csv(p)
    assert out["m1"].home_score == 2 and out["m1"].ht_home == 1
    assert out["m2"].home_score == 0 and out["m2"].ht_home is None


def test_read_results_csv_skips_unfilled_rows(tmp_path: Path):
    p = tmp_path / "r.csv"
    # Extra 'matchup' column + a blank/unplayed row must be tolerated.
    p.write_text(
        "match_id,matchup,home_score,away_score,ht_home,ht_away\n"
        "m1,A vs B,2,1,,\n"
        "m2,C vs D,,,,\n",  # not played yet -> skipped
        encoding="utf-8",
    )
    out = read_results_csv(p)
    assert set(out) == {"m1"}
    assert out["m1"].home_score == 2 and out["m1"].away_score == 1


def test_full_save_grade_roundtrip(tmp_path: Path):
    store = LocalReportStore(tmp_path)
    store.save_report(_report())
    loaded = store.load_report(date(2026, 6, 21))
    g = grade(loaded, {"m1": MatchResultInput(match_id="m1", home_score=1, away_score=0)})
    assert g.wins == 1 and g.total_pnl_cents == 62
