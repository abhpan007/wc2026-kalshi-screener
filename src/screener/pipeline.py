"""End-to-end daily pipeline orchestration.

Wires the stages into one function: discover today's World Cup matches from
Kalshi (series-driven; see ``kalshi_markets``), attach de-vigged reference lines
from the odds source and team news, price every mapped market with the Poisson
engine, screen for divergences, and assemble a :class:`DailyReport`.

Design choices:
  - Clients are INJECTED (the read-only ABCs), so the whole pipeline is testable
    with in-memory fakes and never needs the network in CI.
  - Every stage degrades gracefully: a missing odds/news source, an unmatched
    match, or a parse failure marks that input missing and the run continues.
    Only Kalshi (the market source) is load-bearing.
  - Match identity comes from the Kalshi match (team pair + ticker date), never a
    "matchday" string. Kalshi↔odds matching is by the normalized {home, away} set
    (a known weak spot when the two feeds name a country differently).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

import structlog

from .clients.kalshi import MarketDataClient
from .clients.kalshi_markets import DiscoveredMatch, discover_matches, parse_matchup  # noqa: F401
from .clients.news import NewsClient, StubNewsClient
from .clients.odds import OddsDataClient
from .config import Settings
from .countries import canonical_country
from .models import (
    CENTRAL,
    UTC,
    Match,
    MatchResultSelection,
    ReferenceLines,
    Team,
    TeamNews,
    XgStrategy,
)
from .pricing.engine import price_match
from .pricing.xg import book_anchored
from .report import DailyReport, MatchReport
from .screening import screen_match

log = structlog.get_logger(__name__)


def _team(name: str) -> Team:
    # team_id is the canonical country token so the same nation has a stable id
    # regardless of which feed's spelling produced it; name keeps the original.
    return Team(team_id=canonical_country(name), name=name)


def _team_key(home_name: str, away_name: str) -> frozenset[str]:
    # Canonicalize so Kalshi/Odds spelling differences (Congo DR vs DR Congo,
    # IR Iran vs Iran, ...) resolve to the same key and the match lines up.
    return frozenset({canonical_country(home_name), canonical_country(away_name)})


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _index_reference_by_team(
    odds: OddsDataClient, sport: str
) -> tuple[dict[frozenset[str], ReferenceLines], dict[frozenset[str], Optional[datetime]]]:
    """Fetch odds and index ReferenceLines + kickoff by the {home, away} team set.
    Empty on any failure — the pipeline then proceeds with no reference lines."""
    ref_by_team: dict[frozenset[str], ReferenceLines] = {}
    kickoff_by_team: dict[frozenset[str], Optional[datetime]] = {}
    try:
        events = odds.fetch_events(sport=sport)
    except Exception as exc:
        log.warning("pipeline.odds_unavailable", error=str(exc))
        return ref_by_team, kickoff_by_team
    from .clients.odds import reference_lines_from_event  # local import: pure fn

    for ev in events:
        if not ev.home_team or not ev.away_team:
            continue
        key = _team_key(ev.home_team, ev.away_team)
        try:
            ref_by_team[key] = reference_lines_from_event(ev)
        except Exception as exc:
            log.warning("pipeline.reference_failed", odds_event=ev.id, error=str(exc))
            ref_by_team[key] = ReferenceLines()
        kickoff_by_team[key] = _parse_iso(ev.commence_time)
    return ref_by_team, kickoff_by_team


def _kalshi_1x2_prices(selections) -> dict[str, Optional[int]]:
    out: dict[str, Optional[int]] = {}
    for s in selections:
        if isinstance(s, MatchResultSelection):
            out[s.outcome] = s.kalshi_price_cents
    return out


def _lambdas(reference: ReferenceLines, settings: Settings):
    """Apply the configured xG strategy. Only book_anchored is wired; form_blend
    needs team scoring/conceding stats that aren't sourced yet."""
    if settings.xg_strategy == XgStrategy.BOOK_ANCHORED:
        return book_anchored(reference, max_goals=settings.max_goals)
    log.warning(
        "pipeline.strategy_unwired",
        strategy=settings.xg_strategy.value,
        reason="form_blend needs team form stats not yet sourced; match unpriceable",
    )
    return None


def _match_day(
    dm: DiscoveredMatch, odds_kickoff: Optional[datetime]
) -> Optional[date]:
    """The match's America/Chicago calendar day for filtering: prefer the precise
    odds kickoff, fall back to the Kalshi ticker date, else unknown (None)."""
    if odds_kickoff is not None:
        return odds_kickoff.astimezone(CENTRAL).date()
    return dm.match_date


def _process_discovered(
    dm: DiscoveredMatch,
    *,
    settings: Settings,
    ref_by_team: dict[frozenset[str], ReferenceLines],
    kickoff_by_team: dict[frozenset[str], Optional[datetime]],
    news_client: NewsClient,
) -> MatchReport:
    home, away = _team(dm.home_name), _team(dm.away_name)
    key = _team_key(dm.home_name, dm.away_name)

    reference = ref_by_team.get(key, ReferenceLines())
    odds_kickoff = kickoff_by_team.get(key)
    # Kickoff: precise from odds; else noon UTC on the ticker date; else now.
    if odds_kickoff is not None:
        kickoff, note = odds_kickoff, ""
    elif dm.match_date is not None:
        kickoff = datetime.combine(dm.match_date, time(12, 0), tzinfo=UTC)
        note = "kickoff approximate (odds unmatched; date from Kalshi ticker)"
    else:
        kickoff = datetime.now(tz=UTC)
        note = "kickoff unknown"

    match = Match(match_id=dm.match_key, home=home, away=away, kickoff_utc=kickoff)

    try:
        home_news, away_news = news_client.get_match_news(home.team_id, away.team_id)
    except Exception as exc:  # an interface impl should not raise, but be safe
        log.warning("pipeline.news_failed", error=str(exc))
        home_news = away_news = TeamNews(known=False)

    lambdas = _lambdas(reference, settings)
    fair_values = (
        price_match(
            dm.selections,
            lambdas,
            news_known=home_news.known and away_news.known,
            num_books=reference.num_books,
            first_half_fraction=settings.first_half_fraction,
            extra_time_fraction=settings.extra_time_fraction,
            penalty_split_home=settings.penalty_split_home,
            max_goals=settings.max_goals,
        )
        if lambdas is not None
        else []
    )

    screen = screen_match(
        fair_values,
        threshold_cents=settings.threshold_cents,
        home_news=home_news,
        away_news=away_news,
    )

    return MatchReport(
        match=match,
        reference=reference,
        home_news_known=home_news.known,
        away_news_known=away_news.known,
        lambdas=lambdas,
        screen=screen,
        kalshi_1x2=_kalshi_1x2_prices(dm.selections),
        unmapped_count=len(dm.unmapped),
        note=note,
    )


def build_daily_report(
    target_date: date,
    settings: Settings,
    *,
    kalshi: MarketDataClient,
    odds: OddsDataClient,
    news: Optional[NewsClient] = None,
    odds_sport: str = "soccer_fifa_world_cup",
    kalshi_series: Optional[list[str]] = None,
    kalshi_statuses: tuple[str, ...] = ("open",),
) -> DailyReport:
    """Run the full pipeline for one match-day and return a DailyReport.

    Matches are filtered to ``target_date`` by their America/Chicago day (odds
    kickoff if matched, else the Kalshi ticker date). Matches with no date signal
    at all are included (we can't rule them out) and flagged in their section.
    """
    news_client = news or StubNewsClient()
    log.info("pipeline.start", date=target_date.isoformat(), strategy=settings.xg_strategy.value)

    # Odds first so each discovered match can look up its reference + kickoff.
    ref_by_team, kickoff_by_team = _index_reference_by_team(odds, odds_sport)

    try:
        discovered = discover_matches(kalshi, series=kalshi_series, statuses=kalshi_statuses)
    except Exception as exc:
        log.error("pipeline.kalshi_unavailable", error=str(exc))
        discovered = []

    reports: list[MatchReport] = []
    for dm in discovered:
        odds_kickoff = kickoff_by_team.get(_team_key(dm.home_name, dm.away_name))
        day = _match_day(dm, odds_kickoff)
        if day is not None and day != target_date:
            continue
        try:
            reports.append(
                _process_discovered(
                    dm,
                    settings=settings,
                    ref_by_team=ref_by_team,
                    kickoff_by_team=kickoff_by_team,
                    news_client=news_client,
                )
            )
        except Exception as exc:
            log.warning("pipeline.match_failed", match=dm.match_key, error=str(exc))

    log.info(
        "pipeline.done",
        n_matches=len(reports),
        n_flagged=sum(len(r.screen.flagged) for r in reports),
    )
    return DailyReport(
        report_date=target_date,
        strategy=settings.xg_strategy,
        threshold_cents=settings.threshold_cents,
        generated_at=datetime.now(tz=CENTRAL),
        matches=reports,
    )
