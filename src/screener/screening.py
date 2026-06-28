"""Divergence screening, ranking, correlation grouping, and prop suppression.

This is the integration stage: it takes the pricing engine's :class:`FairValue`
list for one match (each carrying the Kalshi price on its selection) plus team
news, and produces a ranked set of flagged edges with transparent rationale.

Pure functions only — no I/O. The report stage renders what this returns.

Four jobs:
  1. SCREEN: gap = |fair - kalshi| in cents; flag when gap >= threshold. The
     edge SIDE is Yes when the model thinks Yes is underpriced, else No.
  2. RANK: by gap, DOWN-WEIGHTED by confidence, so a smaller high-confidence edge
     can outrank a larger low-confidence one (the formula is exposed for the
     report; see RANK_WEIGHTS / ranking_formula).
  3. CORRELATE: group flagged edges that are the same directional bet (e.g.
     several low-scoring markets) so they're treated as one position, not many.
  4. SUPPRESS PROPS: Kalshi settles player props at the last fair price before a
     player is ruled out, NOT to No — so an injury-driven prop is not a real
     edge once the news is public. When news says a prop's player is out/doubtful
     we suppress it with an explanatory note. Game-level markets are unaffected.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    AdvanceSelection,
    BttsSelection,
    Confidence,
    CornersSelection,
    CorrectScoreSelection,
    FairValue,
    MatchResultSelection,
    OverUnderSelection,
    PlayerPropSelection,
    TeamNews,
    TeamTotalSelection,
)

log = structlog.get_logger(__name__)

# Confidence down-weighting for ranking. Chosen so the spec's worked example
# holds: a 4-cent HIGH-confidence edge (4 * 1.0 = 4.0) outranks a 5-cent
# LOW-confidence edge (5 * 0.45 = 2.25). Exposed so the report can print it.
RANK_WEIGHTS: dict[Confidence, float] = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.7,
    Confidence.LOW: 0.45,
}


def ranking_formula() -> str:
    """Human-readable description of the ranking logic (for the report footer)."""
    w = ", ".join(f"{c.value}={RANK_WEIGHTS[c]:g}" for c in Confidence)
    return f"rank score = gap(cents) x confidence weight ({w}); higher ranks first"


class ScreenStatus(str, Enum):
    FLAGGED = "flagged"  # a real edge: gap >= threshold
    BELOW_THRESHOLD = "below_threshold"  # priced but gap too small
    EXCLUDED = "excluded"  # not priced by the goal model (e.g. corners)
    SUPPRESSED = "suppressed"  # prop whose player is out/doubtful — not a real edge
    NO_PRICE = "no_price"  # no Kalshi price to compare against


class EdgeSide(str, Enum):
    YES = "yes"
    NO = "no"


class ScreenedMarket(BaseModel):
    """One market after screening, fully self-describing for the report."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    fair_value: FairValue
    status: ScreenStatus
    kalshi_price_cents: Optional[int] = None
    fair_price_cents: Optional[int] = None
    gap_cents: Optional[int] = None
    side: Optional[EdgeSide] = None
    confidence: Optional[Confidence] = None
    rank_score: float = 0.0
    # Directional tags used for correlation grouping (None when not applicable).
    scoring_direction: Optional[str] = None  # "high_scoring" | "low_scoring"
    result_direction: Optional[str] = None  # "favors_home" | "favors_away"
    rationale: str = ""
    note: str = ""


class CorrelationGroup(BaseModel):
    """A set of flagged edges that are really one directional bet."""

    axis: str  # "scoring" | "result"
    direction: str  # e.g. "low_scoring"
    market_ids: list[str]
    note: str


class MatchScreen(BaseModel):
    """Screening result for one match."""

    flagged: list[ScreenedMarket] = Field(default_factory=list)  # sorted, best first
    other: list[ScreenedMarket] = Field(default_factory=list)
    correlation_groups: list[CorrelationGroup] = Field(default_factory=list)

    @property
    def has_edges(self) -> bool:
        return bool(self.flagged)


# --------------------------------------------------------------------------- #
# Player-name matching for prop suppression (provisional; see news provider)
# --------------------------------------------------------------------------- #
def _normalize_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", no_accents.lower())).strip()


def _names_match(prop_player: str, news_player: str) -> bool:
    """Conservative fuzzy match between a prop's player and a news entry.

    Matches when normalized full names are equal, or when one is a substring of
    the other (handles "Pulisic" vs "Christian Pulisic"), or when the last name
    matches AND a first-initial is consistent (handles "C. Pulisic"). Last-name
    sharing alone is NOT enough — that risks false positives between players who
    share a surname. PROVISIONAL: tune once a real news provider's name format is
    known.
    """
    a, b = _normalize_name(prop_player), _normalize_name(news_player)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    a_parts, b_parts = a.split(), b.split()
    if a_parts[-1] == b_parts[-1]:  # same surname; require initial agreement
        return a_parts[0][0] == b_parts[0][0]
    return False


def _prop_player_status(player: str, news: TeamNews) -> Optional[str]:
    """Return 'out' / 'doubtful' / None for a prop player given (known) news."""
    if not news.known:
        return None
    if any(_names_match(player, n) for n in news.players_out):
        return "out"
    if any(_names_match(player, n) for n in news.players_doubtful):
        return "doubtful"
    return None


# --------------------------------------------------------------------------- #
# Directional tagging (for correlation grouping)
# --------------------------------------------------------------------------- #
HIGH, LOW = "high_scoring", "low_scoring"
FAVORS_HOME, FAVORS_AWAY = "favors_home", "favors_away"


def _backed_is_yes(side: EdgeSide) -> bool:
    return side == EdgeSide.YES


def _directions(fv: FairValue, side: EdgeSide) -> tuple[Optional[str], Optional[str]]:
    """(scoring_direction, result_direction) for the bet actually being made.

    The "backed outcome" is the market's Yes outcome when the edge side is Yes,
    else its complement. We tag the scoring axis for totals/team-totals/BTTS and
    (conservatively) extreme correct scores; the result axis only for an
    unambiguous Yes on a team's win.
    """
    sel = fv.selection
    yes = _backed_is_yes(side)

    if isinstance(sel, OverUnderSelection):
        backed_over = (sel.side == "over") == yes
        return (HIGH if backed_over else LOW), None
    if isinstance(sel, TeamTotalSelection):
        backed_over = (sel.side == "over") == yes
        return (HIGH if backed_over else LOW), None
    if isinstance(sel, BttsSelection):
        backed_yes_btts = (sel.outcome == "yes") == yes
        # BTTS-Yes leans higher-scoring; BTTS-No leans lower. A proxy, documented.
        return (HIGH if backed_yes_btts else LOW), None
    if isinstance(sel, CorrectScoreSelection) and yes:
        total = sel.home_score + sel.away_score
        if total <= 1:
            return LOW, None
        if total >= 4:
            return HIGH, None
        return None, None
    if isinstance(sel, MatchResultSelection) and yes:
        if sel.outcome == "home":
            return None, FAVORS_HOME
        if sel.outcome == "away":
            return None, FAVORS_AWAY
    if isinstance(sel, AdvanceSelection) and yes:
        return None, (FAVORS_HOME if sel.team == "home" else FAVORS_AWAY)
    return None, None


# --------------------------------------------------------------------------- #
# Rationale (factual, derived from the numbers — never narrative)
# --------------------------------------------------------------------------- #
def _selection_label(fv: FairValue) -> str:
    sel = fv.selection
    period = "" if sel.period.value == "full" else f" [{sel.period.value}]"
    if isinstance(sel, MatchResultSelection):
        return f"1X2 {sel.outcome}{period}"
    if isinstance(sel, OverUnderSelection):
        return f"{sel.side} {sel.line:g}{period}"
    if isinstance(sel, TeamTotalSelection):
        return f"{sel.team} {sel.side} {sel.line:g}{period}"
    if isinstance(sel, BttsSelection):
        return f"BTTS {sel.outcome}{period}"
    if isinstance(sel, CorrectScoreSelection):
        return f"correct score {sel.home_score}-{sel.away_score}{period}"
    if isinstance(sel, AdvanceSelection):
        return f"{sel.team} advances"
    if isinstance(sel, CornersSelection):
        return f"corners ({sel.description})"
    if isinstance(sel, PlayerPropSelection):
        return f"prop: {sel.player}"
    return "market"


def _rationale(label: str, kalshi: int, fair: int, gap: int, side: EdgeSide) -> str:
    return (
        f"Kalshi {label} at {kalshi}, model fair ~{fair} "
        f"(gap {gap}); edge is to buy {side.value.upper()}."
    )


# --------------------------------------------------------------------------- #
# Screening
# --------------------------------------------------------------------------- #
def _screen_one(
    fv: FairValue, *, threshold_cents: int, news: TeamNews
) -> ScreenedMarket:
    sel = fv.selection

    # Player props: never an edge when the player is out/doubtful (Kalshi settles
    # at last fair price, not to No). Game-level markets are unaffected.
    if isinstance(sel, PlayerPropSelection):
        status_word = _prop_player_status(sel.player, news)
        if status_word is not None:
            return ScreenedMarket(
                fair_value=fv,
                status=ScreenStatus.SUPPRESSED,
                kalshi_price_cents=sel.kalshi_price_cents,
                note=(
                    f"prop suppressed: {sel.player} is {status_word} per team news. "
                    "Kalshi settles props at the last fair price before a player is "
                    "ruled out (not to No), so an injury-driven gap here is NOT a real edge."
                ),
            )
        # No adverse news: still not priced by the goal model.
        return ScreenedMarket(
            fair_value=fv,
            status=ScreenStatus.EXCLUDED,
            kalshi_price_cents=sel.kalshi_price_cents,
            note="player prop not priced by goal model" + ("" if news.known else "; team news unknown"),
        )

    if fv.excluded or not fv.priced or fv.fair_price_cents is None:
        return ScreenedMarket(
            fair_value=fv,
            status=ScreenStatus.EXCLUDED,
            kalshi_price_cents=sel.kalshi_price_cents,
            note=fv.note or "not priced by goal model",
        )

    kalshi = sel.kalshi_price_cents
    if kalshi is None:
        return ScreenedMarket(
            fair_value=fv,
            status=ScreenStatus.NO_PRICE,
            fair_price_cents=fv.fair_price_cents,
            confidence=fv.confidence,
            note="no Kalshi price to compare",
        )

    fair = fv.fair_price_cents
    gap = abs(fair - kalshi)
    side = EdgeSide.YES if fair >= kalshi else EdgeSide.NO
    confidence = fv.confidence or Confidence.LOW
    label = _selection_label(fv)

    if gap < threshold_cents:
        return ScreenedMarket(
            fair_value=fv,
            status=ScreenStatus.BELOW_THRESHOLD,
            kalshi_price_cents=kalshi,
            fair_price_cents=fair,
            gap_cents=gap,
            side=side,
            confidence=confidence,
            note=f"gap {gap} < threshold {threshold_cents}",
        )

    scoring_dir, result_dir = _directions(fv, side)
    return ScreenedMarket(
        fair_value=fv,
        status=ScreenStatus.FLAGGED,
        kalshi_price_cents=kalshi,
        fair_price_cents=fair,
        gap_cents=gap,
        side=side,
        confidence=confidence,
        rank_score=gap * RANK_WEIGHTS[confidence],
        scoring_direction=scoring_dir,
        result_direction=result_dir,
        rationale=_rationale(label, kalshi, fair, gap, side),
    )


def _correlation_groups(flagged: list[ScreenedMarket]) -> list[CorrelationGroup]:
    """Group flagged edges sharing a direction on the same axis (>= 2 members)."""
    groups: list[CorrelationGroup] = []
    for axis, attr in (("scoring", "scoring_direction"), ("result", "result_direction")):
        by_dir: dict[str, list[str]] = {}
        for m in flagged:
            d = getattr(m, attr)
            if d is None:
                continue
            mid = m.fair_value.selection.market_id or _selection_label(m.fair_value)
            by_dir.setdefault(d, []).append(mid)
        for direction, ids in by_dir.items():
            if len(ids) >= 2:
                groups.append(
                    CorrelationGroup(
                        axis=axis,
                        direction=direction,
                        market_ids=ids,
                        note=(
                            f"{len(ids)} flagged edges all point {direction.replace('_', ' ')}; "
                            "these are one correlated bet, not independent edges — size accordingly."
                        ),
                    )
                )
    return groups


def screen_match(
    fair_values: list[FairValue],
    *,
    threshold_cents: int = 3,
    home_news: Optional[TeamNews] = None,
    away_news: Optional[TeamNews] = None,
) -> MatchScreen:
    """Screen one match's fair values into ranked edges + correlation warnings.

    News for prop suppression is the union of both teams' news (a prop's player
    may be on either side). Missing news degrades gracefully to "unknown", in
    which case props are simply excluded rather than suppressed.
    """
    h = home_news or TeamNews()
    a = away_news or TeamNews()
    merged_news = TeamNews(
        known=h.known or a.known,
        players_out=[*h.players_out, *a.players_out],
        players_doubtful=[*h.players_doubtful, *a.players_doubtful],
    )

    screened = [
        _screen_one(fv, threshold_cents=threshold_cents, news=merged_news)
        for fv in fair_values
    ]
    flagged = [m for m in screened if m.status == ScreenStatus.FLAGGED]
    # Rank: score desc, then raw gap desc, then confidence desc — deterministic.
    flagged.sort(
        key=lambda m: (m.rank_score, m.gap_cents or 0, (m.confidence or Confidence.LOW).rank),
        reverse=True,
    )
    other = [m for m in screened if m.status != ScreenStatus.FLAGGED]
    groups = _correlation_groups(flagged)

    log.info(
        "screening.match",
        n_markets=len(fair_values),
        n_flagged=len(flagged),
        n_correlated_groups=len(groups),
    )
    return MatchScreen(flagged=flagged, other=other, correlation_groups=groups)
