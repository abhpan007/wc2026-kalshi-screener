"""Expected-goals (lambda) estimation strategies.

The whole Poisson model hangs off two numbers per match: lambda_home and
lambda_away. How we get them is a *swappable strategy*, config-driven, and we
always record which strategy produced a given match's lambdas.

DEFAULT: ``book_anchored`` — anchor to the market's own goal expectation.
STUB:    ``form_blend``    — placeholder, NOT calibrated, do not trust.
"""

from __future__ import annotations

from typing import Optional

import structlog

from ..models import MatchLambdas, MoneylineProbs, ReferenceLines, XgStrategy
from .poisson import PoissonModel

log = structlog.get_logger(__name__)

# Floors so we never hand the Poisson model a zero/degenerate lambda.
_MIN_TOTAL = 0.2
_MIN_TEAM = 0.05


# --------------------------------------------------------------------------- #
# book_anchored (default)
# --------------------------------------------------------------------------- #
def _total_expectation_from_line(line: float, over_prob: Optional[float]) -> float:
    """Back out total goal expectation (lambda_total) from the book's total.

    If we have the de-vigged over probability, we invert it: find lambda such
    that P(Poisson(lambda) > line) == over_prob. This is the honest reading of
    "the book thinks total goals average X".

    If only the line is available (no price), we approximate lambda_total with
    the line value itself. Rationale: books set the main total at the half-line
    nearest the median, and for these rates the Poisson mean sits within a few
    tenths of the median. This is a documented approximation, flagged in the
    returned note and via lower confidence upstream.
    """
    if over_prob is None:
        return max(line, _MIN_TOTAL)

    # Monotonic in lambda, so bisect. P(total > line) increases with lambda.
    lo, hi = _MIN_TOTAL, 12.0

    def p_over(lam: float) -> float:
        # Over prob for a single Poisson(lam) vs the line.
        from .poisson import poisson_pmf

        # P(X > line) = 1 - P(X <= floor(line))
        import math

        k_max = math.floor(line)
        cdf = sum(poisson_pmf(k, lam) for k in range(k_max + 1))
        return 1.0 - cdf

    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if p_over(mid) < over_prob:
            lo = mid
        else:
            hi = mid
    return max(0.5 * (lo + hi), _MIN_TOTAL)


def _split_via_moneyline(
    total: float, ml: MoneylineProbs, max_goals: int
) -> tuple[float, float]:
    """Split total goals into (home, away) using the de-vigged moneyline.

    We have one free parameter: the supremacy split ``s`` in (0, 1), with
    lambda_home = s * total and lambda_away = (1 - s) * total. We solve for the
    ``s`` whose Poisson model reproduces the book's *win ratio*
    P(home win)/P(away win), by bisection (the model ratio is monotonically
    increasing in s).

    Why the ratio and not the absolute home-win probability: with the total
    fixed we have only one degree of freedom, so we cannot match all of
    home/draw/away. Matching the win ratio is symmetric — a balanced moneyline
    (home == away) yields exactly s = 0.5 — and lets the favorite take the
    larger lambda, which is the property we care about. We deliberately do NOT
    try to match the draw probability; that is the known cost of a 1-parameter
    split (and compounds the Poisson-independence draw understatement). This is
    the documented "split via moneyline" mapping from the spec.
    """
    # Book win ratio (home decisive vs away decisive). Floors avoid divide-by-zero
    # on degenerate inputs.
    target_ratio = max(ml.home, 1e-9) / max(ml.away, 1e-9)

    def model_ratio(s: float) -> float:
        lh = max(s * total, _MIN_TEAM)
        la = max((1.0 - s) * total, _MIN_TEAM)
        r = PoissonModel(lh, la, max_goals=max_goals).result_1x2()
        return max(r.home, 1e-12) / max(r.away, 1e-12)

    lo, hi = 0.01, 0.99
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if model_ratio(mid) < target_ratio:
            lo = mid
        else:
            hi = mid
    s = 0.5 * (lo + hi)
    lh = max(s * total, _MIN_TEAM)
    la = max((1.0 - s) * total, _MIN_TEAM)
    return lh, la


def book_anchored(refs: ReferenceLines, max_goals: int = 15) -> Optional[MatchLambdas]:
    """Default strategy. Needs a total line and a de-vigged moneyline.

    Returns ``None`` if the required book inputs are missing — the caller then
    either falls back to another strategy or marks the match unpriceable,
    rather than this function inventing numbers.
    """
    if refs.total_line is None or refs.moneyline is None:
        log.info("book_anchored.skip", reason="missing total_line or moneyline")
        return None

    total = _total_expectation_from_line(refs.total_line, refs.over_prob)
    lh, la = _split_via_moneyline(total, refs.moneyline, max_goals)

    note_bits = [f"total≈{total:.2f} from line {refs.total_line:g}"]
    note_bits.append("over_prob inverted" if refs.over_prob is not None else "line-as-mean approx")
    note = "; ".join(note_bits)

    lambdas = MatchLambdas(
        lambda_home=lh,
        lambda_away=la,
        strategy=XgStrategy.BOOK_ANCHORED,
        note=note,
    )
    log.info(
        "xg.book_anchored",
        lambda_home=round(lh, 3),
        lambda_away=round(la, 3),
        total=round(total, 3),
        note=note,
    )
    return lambdas


# --------------------------------------------------------------------------- #
# form_blend (STUB — not calibrated)
# --------------------------------------------------------------------------- #
def form_blend(
    home_scored: float,
    home_conceded: float,
    away_scored: float,
    away_conceded: float,
) -> MatchLambdas:
    """STUB strategy. Blends recent scoring/conceding rates.

    lambda_home = mean(home recent scoring, away recent conceding)
    lambda_away = mean(away recent scoring, home recent conceding)

    TODO: opponent-strength adjustment (normalize each team's rates by the
    quality of opponents faced). Without it these rates are biased by schedule.
    This strategy is a PLACEHOLDER and is NOT calibrated — do not present its
    output as trustworthy. Confidence for matches priced this way is forced LOW
    upstream.
    """
    lh = max(0.5 * (home_scored + away_conceded), _MIN_TEAM)
    la = max(0.5 * (away_scored + home_conceded), _MIN_TEAM)
    log.warning(
        "xg.form_blend.uncalibrated",
        lambda_home=round(lh, 3),
        lambda_away=round(la, 3),
        todo="opponent adjustment not implemented",
    )
    return MatchLambdas(
        lambda_home=lh,
        lambda_away=la,
        strategy=XgStrategy.FORM_BLEND,
        note="UNCALIBRATED stub; no opponent adjustment",
    )
