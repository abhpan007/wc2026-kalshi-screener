"""Turn a match's lambdas + a list of Kalshi selections into fair values.

This layer wraps the pure :class:`PoissonModel` math with the bookkeeping the
spec requires: a confidence tag per fair value, first-half scaling, and clean
handling of market types we deliberately do not price (corners, player props).

Pricing stays a pure function of its inputs — no I/O, no globals.
"""

from __future__ import annotations

import structlog

from ..models import (
    BttsSelection,
    Confidence,
    CornersSelection,
    CorrectScoreSelection,
    FairValue,
    MatchLambdas,
    MatchResultSelection,
    OverUnderSelection,
    Period,
    PlayerPropSelection,
    Selection,
    TeamTotalSelection,
)
from .poisson import DEFAULT_MAX_GOALS, PoissonModel

log = structlog.get_logger(__name__)

# Default fraction of full-game lambda attributed to the first half. Slightly
# under 0.5 because second halves are marginally higher scoring (fatigue, chasing
# the game). This is an APPROXIMATION — a real model would estimate a separate 1H
# rate. Documented here and reflected in lowered confidence for 1H markets.
DEFAULT_FIRST_HALF_FRACTION = 0.45


def assess_confidence(
    *,
    news_known: bool,
    num_books: int,
    period: Period,
    selection: Selection,
) -> Confidence:
    """Confidence reflects INPUT QUALITY, never edge size.

    Base:
        HIGH   if >= 2 reference books and team news is known
        MEDIUM if >= 1 book (news may be missing)
        LOW    if no book input at all
    Then downgrade one notch for approximate market types:
        - first-half markets (1H fraction is an approximation)
        - correct score (sparse, sensitive to the exact lambdas)
    """
    if num_books >= 2 and news_known:
        base = Confidence.HIGH
    elif num_books >= 1:
        base = Confidence.MEDIUM
    else:
        base = Confidence.LOW

    downgrade = period == Period.FIRST_HALF or isinstance(selection, CorrectScoreSelection)
    if downgrade:
        base = {Confidence.HIGH: Confidence.MEDIUM, Confidence.MEDIUM: Confidence.LOW}.get(
            base, Confidence.LOW
        )
    return base


def _prob_for_selection(model: PoissonModel, sel: Selection) -> float:
    """Map a selection to its model probability. Pure dispatch on the union."""
    if isinstance(sel, MatchResultSelection):
        r = model.result_1x2()
        return {"home": r.home, "draw": r.draw, "away": r.away}[sel.outcome]
    if isinstance(sel, OverUnderSelection):
        ou = model.over_under(sel.line)
        return ou.yes if sel.side == "over" else ou.no
    if isinstance(sel, TeamTotalSelection):
        tt = model.team_total(sel.team, sel.line)
        return tt.yes if sel.side == "over" else tt.no
    if isinstance(sel, BttsSelection):
        b = model.btts()
        return b.yes if sel.outcome == "yes" else b.no
    if isinstance(sel, CorrectScoreSelection):
        return model.score_prob(sel.home_score, sel.away_score)
    raise TypeError(f"selection type {type(sel).__name__} is not model-priceable")


def price_selection(
    selection: Selection,
    lambdas: MatchLambdas,
    *,
    news_known: bool,
    num_books: int,
    first_half_fraction: float = DEFAULT_FIRST_HALF_FRACTION,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> FairValue:
    """Price one selection, returning a fully self-describing FairValue.

    Corners and player props are returned unpriced/excluded with an explanatory
    note — they are intentionally outside the goal model's scope. (The injury-
    suppression rule for props lives in the screening stage, which has news in
    context; here we just refuse to fabricate a goal-model price for them.)
    """
    # -- market types we never price from the goal model ------------------- #
    if isinstance(selection, CornersSelection):
        return FairValue(
            selection=selection,
            priced=False,
            excluded=True,
            note="corners have no goal model; excluded from screening",
        )
    if isinstance(selection, PlayerPropSelection):
        return FairValue(
            selection=selection,
            priced=False,
            excluded=True,
            note=(
                "player prop not priced by goal model; Kalshi settles props at "
                "last fair price before a player is ruled out, so injury-driven "
                "gaps are not real edges — see screening stage"
            ),
        )

    # -- scale to the requested period ------------------------------------- #
    if selection.period == Period.FIRST_HALF:
        used = lambdas.scaled(first_half_fraction)
    else:
        used = lambdas

    model = PoissonModel(used.lambda_home, used.lambda_away, max_goals=max_goals)
    prob = _prob_for_selection(model, selection)
    confidence = assess_confidence(
        news_known=news_known,
        num_books=num_books,
        period=selection.period,
        selection=selection,
    )

    return FairValue(
        selection=selection,
        priced=True,
        excluded=False,
        probability=prob,
        fair_price_cents=round(prob * 100),
        lambdas_used=used,
        confidence=confidence,
        note=used.note,
    )


def price_match(
    selections: list[Selection],
    lambdas: MatchLambdas,
    *,
    news_known: bool,
    num_books: int,
    first_half_fraction: float = DEFAULT_FIRST_HALF_FRACTION,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> list[FairValue]:
    """Price every selection for one match. Logs the strategy/lambdas once."""
    log.info(
        "pricing.match",
        strategy=lambdas.strategy.value,
        lambda_home=round(lambdas.lambda_home, 3),
        lambda_away=round(lambdas.lambda_away, 3),
        n_selections=len(selections),
    )
    return [
        price_selection(
            sel,
            lambdas,
            news_known=news_known,
            num_books=num_books,
            first_half_fraction=first_half_fraction,
            max_goals=max_goals,
        )
        for sel in selections
    ]
