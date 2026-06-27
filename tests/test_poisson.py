"""Tests for the Poisson core, checked against hand computation and the analytic
identities the model is built on. This is the file to trust the engine by."""

from __future__ import annotations

import math

import pytest

from screener.pricing.poisson import PoissonModel, poisson_pmf


# --------------------------------------------------------------------------- #
# poisson_pmf
# --------------------------------------------------------------------------- #
def test_pmf_hand_values():
    # P(0; 1) = e^-1
    assert poisson_pmf(0, 1.0) == pytest.approx(math.exp(-1.0))
    # P(1; 1) = e^-1
    assert poisson_pmf(1, 1.0) == pytest.approx(math.exp(-1.0))
    # P(2; 2) = e^-2 * 2^2 / 2! = 2 e^-2
    assert poisson_pmf(2, 2.0) == pytest.approx(2.0 * math.exp(-2.0))
    # P(3; 1.5) = e^-1.5 * 1.5^3 / 6
    assert poisson_pmf(3, 1.5) == pytest.approx(math.exp(-1.5) * 1.5 ** 3 / 6)


def test_pmf_lambda_zero():
    assert poisson_pmf(0, 0.0) == 1.0
    assert poisson_pmf(1, 0.0) == 0.0
    assert poisson_pmf(5, 0.0) == 0.0


def test_pmf_negative_k_is_zero_negative_lambda_raises():
    assert poisson_pmf(-1, 1.0) == 0.0
    with pytest.raises(ValueError):
        poisson_pmf(1, -0.5)


def test_pmf_sums_to_one():
    total = sum(poisson_pmf(k, 2.3) for k in range(40))
    assert total == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# 1X2
# --------------------------------------------------------------------------- #
def test_1x2_sums_to_one():
    r = PoissonModel(1.5, 1.2).result_1x2()
    assert r.home + r.draw + r.away == pytest.approx(1.0, abs=1e-9)


def test_1x2_symmetric_is_balanced():
    r = PoissonModel(1.3, 1.3).result_1x2()
    assert r.home == pytest.approx(r.away, abs=1e-12)
    assert r.draw > 0.0


def test_1x2_favorite_has_higher_win_prob():
    r = PoissonModel(2.0, 0.8).result_1x2()
    assert r.home > r.away


def test_1x2_hand_small_lambda():
    # With tiny lambdas, draw is dominated by 0-0. Spot check 0-0 contributes.
    m = PoissonModel(0.4, 0.3)
    r = m.result_1x2()
    p00 = math.exp(-0.4) * math.exp(-0.3)
    assert r.draw > p00 * 0.99  # draw >= P(0-0)
    assert r.draw < 1.0


# --------------------------------------------------------------------------- #
# Over/Under — checked against total ~ Poisson(lh + la)
# --------------------------------------------------------------------------- #
def test_total_is_poisson_of_sum():
    lh, la = 1.4, 1.1
    lam = lh + la
    m = PoissonModel(lh, la)
    ou = m.over_under(2.5)
    # P(under 2.5) = P(total in {0,1,2}) for Poisson(lam)
    under_hand = sum(poisson_pmf(k, lam) for k in range(3))
    assert ou.no == pytest.approx(under_hand, abs=1e-12)
    assert ou.yes == pytest.approx(1.0 - under_hand, abs=1e-9)
    assert ou.push == 0.0


def test_over_under_half_line_no_push_and_sums():
    ou = PoissonModel(1.7, 1.3).over_under(1.5)
    assert ou.push == 0.0
    assert ou.yes + ou.no == pytest.approx(1.0, abs=1e-9)


def test_over_under_integer_line_has_push():
    lh, la = 1.0, 1.0
    ou = PoissonModel(lh, la).over_under(2.0)
    push_hand = poisson_pmf(2, lh + la)  # P(total == 2)
    assert ou.push == pytest.approx(push_hand, abs=1e-12)
    assert ou.yes + ou.no + ou.push == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Team totals
# --------------------------------------------------------------------------- #
def test_team_total_matches_marginal():
    lh = 1.6
    tt = PoissonModel(lh, 1.0).team_total("home", 1.5)
    over_hand = 1.0 - poisson_pmf(0, lh) - poisson_pmf(1, lh)
    # abs at 1e-9, not 1e-12: team_total sums the grid truncated at max_goals,
    # so it omits the (tiny, ~1e-11) tail beyond max_goals that over_hand keeps.
    assert tt.yes == pytest.approx(over_hand, abs=1e-9)
    assert tt.no == pytest.approx(poisson_pmf(0, lh) + poisson_pmf(1, lh), abs=1e-12)


def test_team_total_bad_team_raises():
    with pytest.raises(ValueError):
        PoissonModel(1.0, 1.0).team_total("middle", 0.5)


# --------------------------------------------------------------------------- #
# BTTS — independence identity
# --------------------------------------------------------------------------- #
def test_btts_independence():
    lh, la = 1.5, 1.2
    b = PoissonModel(lh, la).btts()
    yes_hand = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    assert b.yes == pytest.approx(yes_hand, abs=1e-12)
    assert b.no == pytest.approx(1 - yes_hand, abs=1e-12)


# --------------------------------------------------------------------------- #
# Correct score
# --------------------------------------------------------------------------- #
def test_correct_score_exact():
    lh, la = 1.5, 1.2
    m = PoissonModel(lh, la)
    p11 = poisson_pmf(1, lh) * poisson_pmf(1, la)
    assert m.score_prob(1, 1) == pytest.approx(p11, abs=1e-15)


def test_correct_score_grid_sums_to_one():
    m = PoissonModel(1.5, 1.2, max_goals=20)
    total = sum(m.score_prob(i, j) for i in range(21) for j in range(21))
    assert total == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Construction guards
# --------------------------------------------------------------------------- #
def test_model_rejects_bad_inputs():
    with pytest.raises(ValueError):
        PoissonModel(-1.0, 1.0)
    with pytest.raises(ValueError):
        PoissonModel(1.0, 1.0, max_goals=0)
