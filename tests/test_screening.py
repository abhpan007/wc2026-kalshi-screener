"""Tests for divergence screening, ranking, correlation grouping, prop suppression."""

from __future__ import annotations

from screener.models import (
    BttsSelection,
    Confidence,
    CornersSelection,
    FairValue,
    MatchLambdas,
    MatchResultSelection,
    OverUnderSelection,
    PlayerPropSelection,
    TeamNews,
    TeamTotalSelection,
    XgStrategy,
)
from screener.screening import (
    RANK_WEIGHTS,
    EdgeSide,
    ScreenStatus,
    screen_match,
)

LAM = MatchLambdas(lambda_home=1.5, lambda_away=1.2, strategy=XgStrategy.BOOK_ANCHORED)


def _fv(sel, *, fair_cents, kalshi_cents, confidence=Confidence.HIGH, priced=True, excluded=False):
    sel.market_price_cents = kalshi_cents
    return FairValue(
        selection=sel,
        priced=priced,
        excluded=excluded,
        probability=None if fair_cents is None else fair_cents / 100,
        fair_price_cents=fair_cents,
        lambdas_used=LAM,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# Gap / side / threshold
# --------------------------------------------------------------------------- #
def test_flags_when_gap_meets_threshold():
    fv = _fv(OverUnderSelection(line=2.5, side="over"), fair_cents=53, kalshi_cents=60)
    screen = screen_match([fv], threshold_cents=3)
    assert screen.has_edges
    m = screen.flagged[0]
    assert m.gap_cents == 7
    # fair(53) < kalshi(60): Yes overpriced -> buy No.
    assert m.side == EdgeSide.NO
    assert m.status == ScreenStatus.FLAGGED


def test_buy_yes_when_underpriced():
    fv = _fv(OverUnderSelection(line=2.5, side="over"), fair_cents=60, kalshi_cents=53)
    m = screen_match([fv], threshold_cents=3).flagged[0]
    assert m.side == EdgeSide.YES


def test_below_threshold_not_flagged():
    fv = _fv(OverUnderSelection(line=2.5, side="over"), fair_cents=52, kalshi_cents=50)
    screen = screen_match([fv], threshold_cents=3)
    assert not screen.has_edges
    assert screen.other[0].status == ScreenStatus.BELOW_THRESHOLD


def test_rationale_contains_numbers():
    fv = _fv(OverUnderSelection(line=1.5, side="over"), fair_cents=53, kalshi_cents=60)
    m = screen_match([fv], threshold_cents=3).flagged[0]
    assert "60" in m.rationale and "53" in m.rationale and "7" in m.rationale


# --------------------------------------------------------------------------- #
# Ranking: confidence down-weighting (the spec's worked example)
# --------------------------------------------------------------------------- #
def test_high_confidence_small_edge_outranks_low_confidence_big_edge():
    # 4-cent HIGH total vs 5-cent LOW first-half-ish prop-equivalent.
    high = _fv(
        OverUnderSelection(line=2.5, side="over"),
        fair_cents=54, kalshi_cents=50, confidence=Confidence.HIGH,
    )
    low = _fv(
        TeamTotalSelection(team="home", line=1.5, side="over"),
        fair_cents=55, kalshi_cents=50, confidence=Confidence.LOW,
    )
    screen = screen_match([low, high], threshold_cents=3)
    assert [m.fair_value.selection for m in screen.flagged][0] is high.selection
    # 4*1.0 = 4.0 beats 5*0.45 = 2.25
    assert screen.flagged[0].rank_score == 4 * RANK_WEIGHTS[Confidence.HIGH]
    assert screen.flagged[1].rank_score == 5 * RANK_WEIGHTS[Confidence.LOW]


# --------------------------------------------------------------------------- #
# Correlation grouping
# --------------------------------------------------------------------------- #
def test_correlated_low_scoring_edges_grouped():
    # Two edges that both back "under / low scoring".
    under = OverUnderSelection(line=2.5, side="under")
    under.market_id = "M-UNDER"
    tt_under = TeamTotalSelection(team="home", line=1.5, side="under")
    tt_under.market_id = "M-TTUNDER"
    fvs = [
        _fv(under, fair_cents=58, kalshi_cents=50),       # buy Yes (under) -> LOW
        _fv(tt_under, fair_cents=58, kalshi_cents=50),    # buy Yes (under) -> LOW
    ]
    screen = screen_match(fvs, threshold_cents=3)
    assert len(screen.correlation_groups) == 1
    g = screen.correlation_groups[0]
    assert g.axis == "scoring" and g.direction == "low_scoring"
    assert set(g.market_ids) == {"M-UNDER", "M-TTUNDER"}


def test_opposite_direction_edges_not_grouped():
    over = OverUnderSelection(line=2.5, side="over")
    over.market_id = "OV"
    under_tt = TeamTotalSelection(team="home", line=1.5, side="under")
    under_tt.market_id = "TTU"
    fvs = [
        _fv(over, fair_cents=58, kalshi_cents=50),      # HIGH
        _fv(under_tt, fair_cents=58, kalshi_cents=50),  # LOW
    ]
    screen = screen_match(fvs, threshold_cents=3)
    assert screen.correlation_groups == []


def test_over_overpriced_backs_under_low_scoring():
    # "Over" market but Yes is overpriced -> we buy No = backing the under = LOW.
    over = OverUnderSelection(line=2.5, side="over")
    over.market_id = "OV"
    tt_under = TeamTotalSelection(team="away", line=1.5, side="under")
    tt_under.market_id = "TTU"
    fvs = [
        _fv(over, fair_cents=42, kalshi_cents=50),       # buy No on over -> LOW
        _fv(tt_under, fair_cents=58, kalshi_cents=50),   # buy Yes under -> LOW
    ]
    screen = screen_match(fvs, threshold_cents=3)
    assert len(screen.correlation_groups) == 1
    assert screen.correlation_groups[0].direction == "low_scoring"


# --------------------------------------------------------------------------- #
# Prop suppression
# --------------------------------------------------------------------------- #
def _prop(player: str, kalshi=40) -> FairValue:
    sel = PlayerPropSelection(player=player, description="anytime scorer")
    sel.market_price_cents = kalshi
    return FairValue(selection=sel, priced=False, excluded=True, note="prop")


def test_prop_suppressed_when_player_out():
    news = TeamNews(known=True, players_out=["Christian Pulisic"])
    screen = screen_match([_prop("Christian Pulisic")], home_news=news)
    m = screen.other[0]
    assert m.status == ScreenStatus.SUPPRESSED
    assert "last fair price" in m.note.lower()


def test_prop_suppressed_matches_partial_name():
    news = TeamNews(known=True, players_doubtful=["C. Pulisic"])
    screen = screen_match([_prop("Christian Pulisic")], away_news=news)
    assert screen.other[0].status == ScreenStatus.SUPPRESSED
    assert "doubtful" in screen.other[0].note


def test_prop_not_suppressed_when_news_unknown():
    screen = screen_match([_prop("Christian Pulisic")], home_news=TeamNews(known=False))
    m = screen.other[0]
    assert m.status == ScreenStatus.EXCLUDED
    assert "unknown" in m.note


def test_prop_not_suppressed_when_player_fit():
    news = TeamNews(known=True, players_out=["Someone Else"])
    screen = screen_match([_prop("Christian Pulisic")], home_news=news)
    assert screen.other[0].status == ScreenStatus.EXCLUDED


def test_different_surname_not_falsely_matched():
    news = TeamNews(known=True, players_out=["James Rodriguez"])
    screen = screen_match([_prop("Luka Modric")], home_news=news)
    assert screen.other[0].status == ScreenStatus.EXCLUDED


# --------------------------------------------------------------------------- #
# Other statuses
# --------------------------------------------------------------------------- #
def test_corners_excluded():
    sel = CornersSelection(description="over 9.5")
    sel.market_price_cents = 58
    fv = FairValue(selection=sel, priced=False, excluded=True, note="corners no model")
    screen = screen_match([fv])
    assert screen.other[0].status == ScreenStatus.EXCLUDED


def test_no_kalshi_price_is_no_price_status():
    fv = _fv(OverUnderSelection(line=2.5, side="over"), fair_cents=55, kalshi_cents=None)
    screen = screen_match([fv], threshold_cents=3)
    assert screen.other[0].status == ScreenStatus.NO_PRICE


def test_thin_board_has_no_edges():
    fv = _fv(BttsSelection(outcome="yes"), fair_cents=51, kalshi_cents=50)
    screen = screen_match([fv], threshold_cents=3)
    assert not screen.has_edges
