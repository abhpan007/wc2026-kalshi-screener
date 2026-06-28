"""Tests for settling selections against final results."""

from __future__ import annotations

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
from screener.settle import MatchResultInput, yes_resolves

R = MatchResultInput(match_id="m", home_score=2, away_score=1)  # total 3


def test_match_result():
    assert yes_resolves(MatchResultSelection(outcome="home"), R) is True
    assert yes_resolves(MatchResultSelection(outcome="draw"), R) is False
    assert yes_resolves(MatchResultSelection(outcome="away"), R) is False
    draw = MatchResultInput(match_id="m", home_score=1, away_score=1)
    assert yes_resolves(MatchResultSelection(outcome="draw"), draw) is True


def test_over_under_halfline():
    assert yes_resolves(OverUnderSelection(line=2.5, side="over"), R) is True
    assert yes_resolves(OverUnderSelection(line=2.5, side="under"), R) is False
    assert yes_resolves(OverUnderSelection(line=3.5, side="under"), R) is True


def test_over_under_integer_line_pushes_to_none():
    # total == 3 exactly on the line -> push -> ungradeable.
    assert yes_resolves(OverUnderSelection(line=3.0, side="over"), R) is None


def test_team_total():
    assert yes_resolves(TeamTotalSelection(team="home", line=1.5, side="over"), R) is True
    assert yes_resolves(TeamTotalSelection(team="away", line=1.5, side="over"), R) is False
    assert yes_resolves(TeamTotalSelection(team="away", line=1.0, side="over"), R) is None  # away=1 push


def test_btts():
    assert yes_resolves(BttsSelection(outcome="yes"), R) is True
    assert yes_resolves(BttsSelection(outcome="no"), R) is False
    nil = MatchResultInput(match_id="m", home_score=2, away_score=0)
    assert yes_resolves(BttsSelection(outcome="yes"), nil) is False
    assert yes_resolves(BttsSelection(outcome="no"), nil) is True


def test_correct_score():
    assert yes_resolves(CorrectScoreSelection(home_score=2, away_score=1), R) is True
    assert yes_resolves(CorrectScoreSelection(home_score=1, away_score=1), R) is False


def test_first_half_needs_ht_scores():
    sel = OverUnderSelection(line=0.5, side="over", period=Period.FIRST_HALF)
    assert yes_resolves(sel, R) is None  # no HT scores -> ungradeable
    with_ht = MatchResultInput(match_id="m", home_score=2, away_score=1, ht_home=1, ht_away=0)
    assert yes_resolves(sel, with_ht) is True  # 1 goal in 1H > 0.5


def test_advance_needs_advanced_field():
    # Without the 'advanced' outcome, advance is ungradeable (90' score isn't enough).
    assert yes_resolves(AdvanceSelection(team="home"), R) is None
    won = MatchResultInput(match_id="m", home_score=1, away_score=1, advanced="home")
    assert yes_resolves(AdvanceSelection(team="home"), won) is True
    assert yes_resolves(AdvanceSelection(team="away"), won) is False


def test_corners_and_unpriceable_return_none():
    assert yes_resolves(CornersSelection(description="x"), R) is None
