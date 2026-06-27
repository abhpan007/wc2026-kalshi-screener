"""Independent-Poisson scoreline model.

This is the core of the screener and the part most carefully tested. It takes
two expected-goals values (one per team) and derives every game-level market
probability by summing over the joint scoreline distribution.

KEY MODELING ASSUMPTION — INDEPENDENCE
======================================
We treat home goals and away goals as two *independent* Poisson variables:

    P(home = i, away = j) = Poisson(i; lambda_home) * Poisson(j; lambda_away)

Real matches have mild positive/negative dependence (game state, red cards,
parking-the-bus). The practical consequences, which we flag in the report:
  - draws are slightly understated,
  - "both teams to score = No" is slightly understated.
A Dixon-Coles low-score correction would address this; it is intentionally NOT
applied here so the core stays simple and hand-checkable. See README.

No scipy dependency: the Poisson PMF is computed directly. We truncate the
scoreline grid at ``max_goals``; for realistic football lambdas (~0.3-2.5) the
truncated tail beyond 15 goals per team is < 1e-9, which is below the cent
rounding we ultimately report. We do NOT renormalize, so probabilities match
the textbook Poisson values exactly (tested against hand computation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

DEFAULT_MAX_GOALS = 15


def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for X ~ Poisson(lam).

    Handles lam == 0 cleanly (all mass at k = 0). Raises on negative inputs
    rather than silently returning garbage.
    """
    if lam < 0:
        raise ValueError(f"lambda must be >= 0, got {lam}")
    if k < 0:
        return 0.0
    # Direct formula. lam == 0 works: 0**0 == 1, exp(0) == 1, so pmf(0)=1.
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


class Result1X2(NamedTuple):
    home: float
    draw: float
    away: float


class TwoWay(NamedTuple):
    """Generic two-outcome split (e.g. over/under, btts yes/no)."""

    yes: float  # over / yes
    no: float  # under / no
    push: float = 0.0  # nonzero only for integer over/under lines


@dataclass(frozen=True)
class PoissonModel:
    """An independent-Poisson model for one match (or half).

    Build once from two lambdas, then query each market type. The per-team PMF
    vectors are computed eagerly; the joint grid is summed lazily per query so
    we never materialize a full matrix when a marginal will do.
    """

    lambda_home: float
    lambda_away: float
    max_goals: int = DEFAULT_MAX_GOALS

    def __post_init__(self) -> None:
        if self.lambda_home < 0 or self.lambda_away < 0:
            raise ValueError("lambdas must be >= 0")
        if self.max_goals < 1:
            raise ValueError("max_goals must be >= 1")
        # Cache the marginal PMF vectors (index = goal count).
        object.__setattr__(
            self, "_home_pmf", [poisson_pmf(i, self.lambda_home) for i in range(self.max_goals + 1)]
        )
        object.__setattr__(
            self, "_away_pmf", [poisson_pmf(j, self.lambda_away) for j in range(self.max_goals + 1)]
        )

    # -- marginals ---------------------------------------------------------- #
    @property
    def home_pmf(self) -> list[float]:
        return self._home_pmf  # type: ignore[attr-defined]

    @property
    def away_pmf(self) -> list[float]:
        return self._away_pmf  # type: ignore[attr-defined]

    def score_prob(self, home_score: int, away_score: int) -> float:
        """P(exact scoreline). Computed from the PMF directly (not the truncated
        grid) so correct-score queries are exact even past ``max_goals``."""
        return poisson_pmf(home_score, self.lambda_home) * poisson_pmf(
            away_score, self.lambda_away
        )

    # -- 1X2 ---------------------------------------------------------------- #
    def result_1x2(self) -> Result1X2:
        """Home win / draw / away win by summing the joint scoreline grid."""
        home = draw = away = 0.0
        for i in range(self.max_goals + 1):
            pi = self.home_pmf[i]
            for j in range(self.max_goals + 1):
                p = pi * self.away_pmf[j]
                if i > j:
                    home += p
                elif i == j:
                    draw += p
                else:
                    away += p
        return Result1X2(home, draw, away)

    # -- total goals over/under -------------------------------------------- #
    def total_goals_pmf(self) -> list[float]:
        """PMF of total goals (home + away).

        Under independence the sum of two Poissons is itself Poisson with rate
        lambda_home + lambda_away. We use that exact identity rather than
        convolving the truncated grid, which keeps over/under exact.
        """
        lam = self.lambda_home + self.lambda_away
        n = 2 * self.max_goals
        return [poisson_pmf(k, lam) for k in range(n + 1)]

    def over_under(self, line: float) -> TwoWay:
        """Over/under for a goal ``line``.

        For the usual half-lines (1.5, 2.5, ...) there is no push. Integer lines
        (rare on Kalshi) produce a push leg, surfaced explicitly so the caller
        can decide how to handle it rather than silently folding it in.
        """
        pmf = self.total_goals_pmf()
        over = under = push = 0.0
        for k, p in enumerate(pmf):
            if k > line:
                over += p
            elif k < line:
                under += p
            else:  # k == line, only possible for integer lines
                push += p
        return TwoWay(yes=over, no=under, push=push)

    # -- team totals -------------------------------------------------------- #
    def team_total(self, team: str, line: float) -> TwoWay:
        """Over/under on a single team's goals."""
        if team == "home":
            pmf = self.home_pmf
        elif team == "away":
            pmf = self.away_pmf
        else:
            raise ValueError(f"team must be 'home' or 'away', got {team!r}")
        over = under = push = 0.0
        for k, p in enumerate(pmf):
            if k > line:
                over += p
            elif k < line:
                under += p
            else:
                push += p
        return TwoWay(yes=over, no=under, push=push)

    # -- both teams to score ----------------------------------------------- #
    def btts(self) -> TwoWay:
        """Both teams to score.

        yes = P(home >= 1) * P(away >= 1) under independence. The independence
        assumption makes BTTS-No slightly understated (see module docstring).
        """
        yes = (1.0 - self.home_pmf[0]) * (1.0 - self.away_pmf[0])
        return TwoWay(yes=yes, no=1.0 - yes)
