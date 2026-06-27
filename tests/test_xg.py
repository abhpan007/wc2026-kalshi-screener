"""Tests for the xG strategies, focused on the book_anchored round-trips."""

from __future__ import annotations

import pytest

from screener.models import MoneylineProbs, ReferenceLines, XgStrategy
from screener.pricing.poisson import PoissonModel
from screener.pricing.xg import (
    _total_expectation_from_line,
    book_anchored,
    form_blend,
)


def test_book_anchored_missing_inputs_returns_none():
    assert book_anchored(ReferenceLines(total_line=2.5)) is None
    assert book_anchored(ReferenceLines(moneyline=MoneylineProbs(home=0.4, draw=0.3, away=0.3))) is None
    assert book_anchored(ReferenceLines()) is None


def test_total_expectation_inversion_roundtrips():
    # Pick a lambda, compute its true over-2.5 prob, then recover the total.
    lam = 2.7
    over = PoissonModel(lam, 0.0).over_under(2.5).yes  # single-Poisson over prob
    recovered = _total_expectation_from_line(2.5, over)
    assert recovered == pytest.approx(lam, abs=1e-3)


def test_total_expectation_line_as_mean_when_no_prob():
    assert _total_expectation_from_line(2.5, None) == pytest.approx(2.5)


def test_book_anchored_symmetric_moneyline_splits_evenly():
    refs = ReferenceLines(
        moneyline=MoneylineProbs(home=0.35, draw=0.30, away=0.35),
        total_line=2.5,
        over_prob=0.5,
        num_books=2,
    )
    lam = book_anchored(refs)
    assert lam is not None
    assert lam.strategy == XgStrategy.BOOK_ANCHORED
    assert lam.lambda_home == pytest.approx(lam.lambda_away, abs=1e-2)


def test_book_anchored_favorite_gets_more_goals():
    refs = ReferenceLines(
        moneyline=MoneylineProbs(home=0.55, draw=0.25, away=0.20),
        total_line=2.5,
        over_prob=0.52,
        num_books=3,
    )
    lam = book_anchored(refs)
    assert lam is not None
    assert lam.lambda_home > lam.lambda_away


def test_book_anchored_reproduces_book_win_ratio():
    ml = MoneylineProbs(home=0.50, draw=0.27, away=0.23)
    refs = ReferenceLines(moneyline=ml, total_line=2.5, over_prob=0.52, num_books=3)
    lam = book_anchored(refs)
    assert lam is not None
    r = PoissonModel(lam.lambda_home, lam.lambda_away).result_1x2()
    # The split is fit to match the book's win ratio (home decisive / away decisive).
    assert r.home / r.away == pytest.approx(ml.home / ml.away, rel=1e-3)


def test_book_anchored_total_tracks_line():
    refs = ReferenceLines(
        moneyline=MoneylineProbs(home=0.4, draw=0.3, away=0.3),
        total_line=3.5,
        over_prob=0.5,  # line ~= median, total expectation near 3.5
        num_books=2,
    )
    lam = book_anchored(refs)
    assert lam is not None
    assert lam.total == pytest.approx(3.5, abs=0.5)


def test_form_blend_is_marked_uncalibrated_stub():
    lam = form_blend(home_scored=1.8, home_conceded=1.0, away_scored=1.2, away_conceded=1.4)
    assert lam.strategy == XgStrategy.FORM_BLEND
    assert lam.lambda_home > 0 and lam.lambda_away > 0
    # lambda_home blends home scoring with away conceding: mean(1.8, 1.4) = 1.6
    assert lam.lambda_home == pytest.approx(1.6)  # mean(1.8, 1.4)
    assert lam.lambda_away == pytest.approx(1.1)  # mean(1.2, 1.0)
