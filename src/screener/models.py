"""Pydantic data models shared across the pipeline.

Everything that crosses a stage boundary is a typed model. The pricing engine
in particular keeps a clean separation between *selections* (a binary Kalshi
market we want a fair value for) and *fair values* (what the model returns).

Only the models needed by deliverables 1 and 2 (scaffold + pricing) are defined
here. Models for raw Kalshi/odds/news responses arrive with their clients.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

CENTRAL = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Confidence(str, Enum):
    """Input-quality tag attached to every fair value.

    Driven by how trustworthy the *inputs* were, not the size of the edge:
    fewer reference books, missing team news, or an approximate market type
    (first half, correct score) all push confidence down.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


class XgStrategy(str, Enum):
    """Which lambda-estimation strategy produced a match's expected goals."""

    BOOK_ANCHORED = "book_anchored"  # default; anchors to the book's own goal expectation
    FORM_BLEND = "form_blend"  # STUB; not calibrated, do not trust


class Period(str, Enum):
    FULL = "full"
    FIRST_HALF = "1H"


# --------------------------------------------------------------------------- #
# Match / reference inputs
# --------------------------------------------------------------------------- #
class Team(BaseModel):
    """A team. ``team_id`` is the stable key the pipeline matches on; ``name``
    is for display only and is never load-bearing."""

    team_id: str
    name: str


class Match(BaseModel):
    """A single World Cup fixture.

    All time comparisons happen in UTC; display happens in America/Chicago.
    A late local kickoff that lands on the next ET day is NOT an error.
    """

    match_id: str
    home: Team
    away: Team
    kickoff_utc: datetime
    venue_canonical: Optional[str] = None

    @field_validator("kickoff_utc")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("kickoff_utc must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    def kickoff_central(self) -> datetime:
        return self.kickoff_utc.astimezone(CENTRAL)


class MoneylineProbs(BaseModel):
    """De-vigged 1X2 probabilities. Must sum to ~1 (the de-vigger normalizes)."""

    home: float = Field(ge=0.0, le=1.0)
    draw: float = Field(ge=0.0, le=1.0)
    away: float = Field(ge=0.0, le=1.0)

    @field_validator("away")
    @classmethod
    def _check_sum(cls, v: float, info) -> float:
        total = info.data.get("home", 0.0) + info.data.get("draw", 0.0) + v
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"de-vigged 1X2 must sum to 1, got {total:.6f}")
        return v


class TeamTotalLine(BaseModel):
    """De-vigged reference for one team's total-goals line.

    Optional, additive extension populated by the OddsClient when a book offers
    team-total markets (often absent from standard feeds). ``over_prob`` is the
    de-vigged probability of that team going over ``line``. Degrades gracefully:
    if no book offers team totals, ``ReferenceLines.team_total_lines`` is empty.
    """

    team: Literal["home", "away"]
    line: float
    over_prob: float = Field(ge=0.0, le=1.0)
    num_books: int = 0


class ReferenceLines(BaseModel):
    """Sharp-book reference for one match.

    ``total_line`` is the consensus total goals line (e.g. 2.5). ``over_prob``
    is the de-vigged probability of going over it, used to back out the goal
    expectation. ``num_books`` feeds the confidence tag.
    """

    moneyline: Optional[MoneylineProbs] = None
    total_line: Optional[float] = None
    over_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    num_books: int = 0
    # Additive: per-team total lines when a book offers them; usually empty.
    team_total_lines: list[TeamTotalLine] = Field(default_factory=list)


class TeamNews(BaseModel):
    """Lineup/injury state. Degrades gracefully: ``known=False`` means the
    NewsClient could not resolve this match, NOT that everyone is fit."""

    known: bool = False
    players_out: list[str] = Field(default_factory=list)
    players_doubtful: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pricing: lambdas
# --------------------------------------------------------------------------- #
class MatchLambdas(BaseModel):
    """Expected goals for each team plus provenance.

    The independence-Poisson model is built directly from these two numbers,
    so this is where the model is right or wrong. We always log the strategy.
    """

    lambda_home: float = Field(gt=0.0)
    lambda_away: float = Field(gt=0.0)
    strategy: XgStrategy
    note: str = ""

    @property
    def total(self) -> float:
        return self.lambda_home + self.lambda_away

    def scaled(self, fraction: float) -> "MatchLambdas":
        """Return lambdas scaled by ``fraction`` (used for the 1H approximation)."""
        return MatchLambdas(
            lambda_home=self.lambda_home * fraction,
            lambda_away=self.lambda_away * fraction,
            strategy=self.strategy,
            note=f"{self.note} (scaled x{fraction:g})".strip(),
        )


# --------------------------------------------------------------------------- #
# Pricing: selections (a binary market we want a fair value for)
# --------------------------------------------------------------------------- #
class _BaseSelection(BaseModel):
    """A binary Kalshi market, normalized to what the model needs to price it.

    ``market_id`` and ``kalshi_price_cents`` are optional here because the
    pricing engine only needs the market *shape*; screening attaches prices.
    """

    market_id: Optional[str] = None
    period: Period = Period.FULL
    kalshi_price_cents: Optional[int] = Field(default=None, ge=0, le=100)


class MatchResultSelection(_BaseSelection):
    kind: Literal["match_result"] = "match_result"
    outcome: Literal["home", "draw", "away"]


class OverUnderSelection(_BaseSelection):
    kind: Literal["over_under"] = "over_under"
    line: float
    side: Literal["over", "under"]


class TeamTotalSelection(_BaseSelection):
    kind: Literal["team_total"] = "team_total"
    team: Literal["home", "away"]
    line: float
    side: Literal["over", "under"]


class BttsSelection(_BaseSelection):
    kind: Literal["btts"] = "btts"
    outcome: Literal["yes", "no"]


class CorrectScoreSelection(_BaseSelection):
    kind: Literal["correct_score"] = "correct_score"
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)


class CornersSelection(_BaseSelection):
    """Corners cannot be priced from a goal model. Carried through unpriced."""

    kind: Literal["corners"] = "corners"
    description: str = ""


class PlayerPropSelection(_BaseSelection):
    """Player props need special handling at screening: Kalshi settles them at
    the last fair price before a player is ruled out, NOT to No. An injury-
    driven divergence is therefore not a real edge once the news is public."""

    kind: Literal["player_prop"] = "player_prop"
    player: str
    description: str = ""


Selection = Annotated[
    Union[
        MatchResultSelection,
        OverUnderSelection,
        TeamTotalSelection,
        BttsSelection,
        CorrectScoreSelection,
        CornersSelection,
        PlayerPropSelection,
    ],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Pricing: output
# --------------------------------------------------------------------------- #
class FairValue(BaseModel):
    """The model's answer for one selection.

    Carries the probability, the lambdas/strategy used, and a confidence tag,
    per the spec's requirement that every fair value be self-describing.
    """

    selection: Selection
    priced: bool
    excluded: bool = False
    probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    fair_price_cents: Optional[int] = Field(default=None, ge=0, le=100)
    lambdas_used: Optional[MatchLambdas] = None
    confidence: Optional[Confidence] = None
    note: str = ""
