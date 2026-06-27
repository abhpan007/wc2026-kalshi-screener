"""Tests for the pricing engine wrapper: fair-value bookkeeping, confidence,
first-half scaling, and exclusion of corners/props."""

from __future__ import annotations

import pytest

from screener.models import (
    BttsSelection,
    Confidence,
    CornersSelection,
    CorrectScoreSelection,
    MatchLambdas,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    PlayerPropSelection,
    XgStrategy,
)
from screener.pricing.engine import assess_confidence, price_match, price_selection
from screener.pricing.poisson import PoissonModel


def _lambdas() -> MatchLambdas:
    return MatchLambdas(
        lambda_home=1.5, lambda_away=1.2, strategy=XgStrategy.BOOK_ANCHORED, note="test"
    )


def test_fair_price_is_probability_in_cents():
    sel = OverUnderSelection(line=2.5, side="over")
    fv = price_selection(sel, _lambdas(), news_known=True, num_books=3)
    assert fv.priced
    expected = PoissonModel(1.5, 1.2).over_under(2.5).yes
    assert fv.probability == pytest.approx(expected)
    assert fv.fair_price_cents == round(expected * 100)


def test_match_result_dispatch():
    fv = price_selection(
        MatchResultSelection(outcome="home"), _lambdas(), news_known=True, num_books=2
    )
    assert fv.probability == pytest.approx(PoissonModel(1.5, 1.2).result_1x2().home)


def test_corners_excluded_unpriced():
    fv = price_selection(
        CornersSelection(description="over 9.5"), _lambdas(), news_known=True, num_books=2
    )
    assert not fv.priced
    assert fv.excluded
    assert fv.probability is None
    assert "corner" in fv.note.lower()


def test_player_prop_excluded_with_settlement_note():
    fv = price_selection(
        PlayerPropSelection(player="X", description="anytime scorer"),
        _lambdas(),
        news_known=True,
        num_books=2,
    )
    assert not fv.priced and fv.excluded
    assert "last fair price" in fv.note.lower()


def test_first_half_scaling_lowers_over_prob():
    sel_full = OverUnderSelection(line=1.5, side="over", period=Period.FULL)
    sel_1h = OverUnderSelection(line=1.5, side="over", period=Period.FIRST_HALF)
    full = price_selection(sel_full, _lambdas(), news_known=True, num_books=2)
    half = price_selection(sel_1h, _lambdas(), news_known=True, num_books=2, first_half_fraction=0.45)
    assert half.probability < full.probability
    # lambdas recorded on the 1H fair value are the scaled ones
    assert half.lambdas_used.lambda_home == pytest.approx(1.5 * 0.45)


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_confidence_high_needs_two_books_and_news():
    c = assess_confidence(
        news_known=True, num_books=2, period=Period.FULL,
        selection=OverUnderSelection(line=2.5, side="over"),
    )
    assert c == Confidence.HIGH


def test_confidence_medium_with_one_book():
    c = assess_confidence(
        news_known=True, num_books=1, period=Period.FULL,
        selection=OverUnderSelection(line=2.5, side="over"),
    )
    assert c == Confidence.MEDIUM


def test_confidence_low_with_no_books():
    c = assess_confidence(
        news_known=False, num_books=0, period=Period.FULL,
        selection=OverUnderSelection(line=2.5, side="over"),
    )
    assert c == Confidence.LOW


def test_confidence_first_half_downgrades():
    c = assess_confidence(
        news_known=True, num_books=2, period=Period.FIRST_HALF,
        selection=OverUnderSelection(line=1.5, side="over"),
    )
    assert c == Confidence.MEDIUM  # would be HIGH at full time


def test_confidence_correct_score_downgrades():
    c = assess_confidence(
        news_known=True, num_books=2, period=Period.FULL,
        selection=CorrectScoreSelection(home_score=1, away_score=1),
    )
    assert c == Confidence.MEDIUM


def test_news_missing_caps_below_high():
    c = assess_confidence(
        news_known=False, num_books=3, period=Period.FULL,
        selection=BttsSelection(outcome="yes"),
    )
    assert c == Confidence.MEDIUM


def test_price_match_prices_all_and_logs_once():
    sels = [
        MatchResultSelection(outcome="home"),
        OverUnderSelection(line=2.5, side="under"),
        CornersSelection(description="x"),
    ]
    out = price_match(sels, _lambdas(), news_known=True, num_books=2)
    assert len(out) == 3
    assert out[0].priced and out[1].priced and not out[2].priced
