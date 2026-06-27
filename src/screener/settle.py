"""Settle a selection against a final match result.

Pure functions used by grading: given the actual score, did the market's YES
outcome resolve true? Returns None when the market can't be graded from the
inputs available (e.g. a first-half market with no half-time score, or an
integer-line push), so the grader can exclude it rather than guess.

Only game-level markets settle here. Corners and player props are never graded
(no goal-model fair value, and props have the special Kalshi settlement rule),
so they return None.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .models import (
    BttsSelection,
    CorrectScoreSelection,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    Selection,
    TeamTotalSelection,
)


class MatchResultInput(BaseModel):
    """A final result for grading. Half-time scores are optional; without them,
    first-half markets are ungradeable (returned as None, not guessed)."""

    match_id: str
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    ht_home: Optional[int] = Field(default=None, ge=0)
    ht_away: Optional[int] = Field(default=None, ge=0)


def _scores_for(selection: Selection, result: MatchResultInput) -> Optional[tuple[int, int]]:
    """(home, away) goals relevant to the selection's period, or None if unknown."""
    if selection.period == Period.FIRST_HALF:
        if result.ht_home is None or result.ht_away is None:
            return None
        return result.ht_home, result.ht_away
    return result.home_score, result.away_score


def yes_resolves(selection: Selection, result: MatchResultInput) -> Optional[bool]:
    """Did the market's YES outcome happen? None if ungradeable.

    Over/under and team totals on an INTEGER line that lands exactly on the
    score are a push (stake returned) — graded as None so they don't count as a
    win or a loss. The usual half-lines never push.
    """
    scores = _scores_for(selection, result)
    if scores is None:
        return None
    h, a = scores
    total = h + a

    if isinstance(selection, MatchResultSelection):
        if selection.outcome == "home":
            return h > a
        if selection.outcome == "draw":
            return h == a
        return h < a  # away

    if isinstance(selection, OverUnderSelection):
        if total == selection.line:
            return None  # integer-line push
        over = total > selection.line
        return over if selection.side == "over" else (not over)

    if isinstance(selection, TeamTotalSelection):
        g = h if selection.team == "home" else a
        if g == selection.line:
            return None  # push
        over = g > selection.line
        return over if selection.side == "over" else (not over)

    if isinstance(selection, BttsSelection):
        both = h >= 1 and a >= 1
        return both if selection.outcome == "yes" else (not both)

    if isinstance(selection, CorrectScoreSelection):
        return h == selection.home_score and a == selection.away_score

    # Corners, player props: not graded.
    return None
