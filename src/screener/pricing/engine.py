"""Turn a match's lambdas + a list of Kalshi selections into fair values.

This layer wraps the pure :class:`PoissonModel` math with the bookkeeping the
spec requires: a confidence tag per fair value, first-half scaling, and clean
handling of market types we deliberately do not price (corners, player props).

Pricing stays a pure function of its inputs — no I/O, no globals.
"""

from __future__ import annotations

import structlog

from ..models import (
    AdvanceSelection,
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

# Knockout "to advance" defaults: extra time ≈ 1/3 of a 90-min game (30 min), and
# a 50/50 penalty shootout. Both are approximations; advance confidence is
# downgraded a notch to reflect them.
DEFAULT_EXTRA_TIME_FRACTION = 1.0 / 3.0
DEFAULT_PENALTY_SPLIT_HOME = 0.5


def advance_probabilities(
    lambdas: MatchLambdas,
    *,
    extra_time_fraction: float = DEFAULT_EXTRA_TIME_FRACTION,
    penalty_split_home: float = DEFAULT_PENALTY_SPLIT_HOME,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> tuple[float, float]:
    """P(home advances), P(away advances) for a knockout tie.

    A team advances by either winning in 90 minutes, or drawing in 90 and then
    winning the extra-time + penalty resolution:

        P(advance) = P(win 90) + P(draw 90) * P(win | level after 90)

    Extra time is modeled as the same independent-Poisson over a fraction of a
    full game; if still level after ET, the shootout is split by
    ``penalty_split_home`` (0.5 = coin flip). The two advance probabilities sum
    to 1 by construction.
    """
    full = PoissonModel(lambdas.lambda_home, lambdas.lambda_away, max_goals=max_goals)
    r = full.result_1x2()

    et = lambdas.scaled(extra_time_fraction)
    et_r = PoissonModel(et.lambda_home, et.lambda_away, max_goals=max_goals).result_1x2()
    # Win from a level game after 90: win in ET, else win the shootout.
    q_home = et_r.home + et_r.draw * penalty_split_home
    q_away = et_r.away + et_r.draw * (1.0 - penalty_split_home)

    p_home = r.home + r.draw * q_home
    p_away = r.away + r.draw * q_away
    # Renormalize: it's a clean 2-way market, so the two sides sum to exactly 1.
    # The only reason they don't is the score-grid truncation (negligible noise).
    total = p_home + p_away
    if total > 0:
        p_home, p_away = p_home / total, p_away / total
    return p_home, p_away


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

    downgrade = (
        period == Period.FIRST_HALF
        or isinstance(selection, (CorrectScoreSelection, AdvanceSelection))
    )
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
    extra_time_fraction: float = DEFAULT_EXTRA_TIME_FRACTION,
    penalty_split_home: float = DEFAULT_PENALTY_SPLIT_HOME,
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

    # -- knockout "to advance" (composes 90' + extra time + penalties) ----- #
    if isinstance(selection, AdvanceSelection):
        p_home, p_away = advance_probabilities(
            lambdas,
            extra_time_fraction=extra_time_fraction,
            penalty_split_home=penalty_split_home,
            max_goals=max_goals,
        )
        prob = p_home if selection.team == "home" else p_away
        return FairValue(
            selection=selection,
            priced=True,
            excluded=False,
            probability=prob,
            fair_price_cents=round(prob * 100),
            lambdas_used=lambdas,
            confidence=assess_confidence(
                news_known=news_known, num_books=num_books,
                period=selection.period, selection=selection,
            ),
            note=f"advance = win90 + draw90×(ET+pens); {lambdas.note}".strip(),
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
    extra_time_fraction: float = DEFAULT_EXTRA_TIME_FRACTION,
    penalty_split_home: float = DEFAULT_PENALTY_SPLIT_HOME,
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
            extra_time_fraction=extra_time_fraction,
            penalty_split_home=penalty_split_home,
            max_goals=max_goals,
        )
        for sel in selections
    ]
