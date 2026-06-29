"""Local entrypoint: ``python -m screener.run --date YYYY-MM-DD``.

This is the single entrypoint the Lambda will also call. It runs the guardrail
check, configures logging, builds the read-only clients, runs the full pipeline,
and renders the daily report (markdown + HTML).

  - Real mode (``--date`` / default today): constructs HTTP clients against the
    live Kalshi and Odds APIs. Kalshi market data is public; the Odds API needs
    SCREENER_ODDS_API_KEY (absent → reference lines degrade to missing). News is
    the stub ("unknown") until a real provider is wired.
  - ``--demo``: runs the WHOLE pipeline offline against synthetic in-memory
    clients and prints a real report — no network, no keys. The demo is seeded
    to show flagged edges, a correlation group, and an injury-suppressed prop.

Persistence to S3 and email via SES arrive with deliverables 8–9; locally we
print the markdown and write markdown + HTML files under ``./reports/``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import requests
import structlog

from .clients.cache import DiskCache
from .clients.http import HttpClient
from .clients.kalshi import (
    PROD_BASE_URL,
    KalshiEvent,
    KalshiHttpClient,
    KalshiMarket,
    MarketDataClient,
)
from .clients.news import NewsClient, StubNewsClient
from .clients.odds import (
    BASE_URL as ODDS_BASE_URL,
    Bookmaker,
    OddsDataClient,
    OddsEvent,
    OddsMarket,
    OddsOutcome,
    TheOddsApiClient,
    WORLD_CUP_SPORT_KEY,
)
from .config import Settings
from .guardrails import assert_read_only
from .logging import configure_logging
from .models import ReferenceLines, TeamNews
from .pipeline import build_daily_report
from .report import render_html, render_markdown
from .storage import LocalReportStore

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Real clients
# --------------------------------------------------------------------------- #
def _build_clients(settings: Settings, day: str) -> tuple[MarketDataClient, OddsDataClient, NewsClient]:
    session = requests.Session()
    root = Path(settings.cache_dir)
    kalshi = KalshiHttpClient(
        HttpClient(PROD_BASE_URL, session=session, cache=DiskCache(root, day, "kalshi"))
    )
    odds = TheOddsApiClient(
        HttpClient(ODDS_BASE_URL, session=session, cache=DiskCache(root, day, "odds"))
    )
    news: NewsClient = StubNewsClient()  # real provider wired later (see README)
    return kalshi, odds, news


# --------------------------------------------------------------------------- #
# Demo clients (offline, deterministic) — exercise the full pipeline
# --------------------------------------------------------------------------- #
_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _demo_datecode(d: date) -> str:
    return f"{d.year % 100:02d}{_MON[d.month - 1]}{d.day:02d}"


class _DemoKalshi(MarketDataClient):
    """Real-shaped: one series per market type, one event per match. Seeded for
    today's date so it passes the day filter, with prices set to flag edges."""

    def __init__(self, target: date) -> None:
        key = f"{_demo_datecode(target)}USAMEX"

        def mk(t, sub, **kw):
            return KalshiMarket(ticker=t, yes_sub_title=sub, **kw)

        def ev(series, title, mkts):
            return KalshiEvent(event_ticker=f"{series}-{key}", series_ticker=series, title=title, markets=mkts)

        self._by_series = {
            "KXWCGAME": [ev("KXWCGAME", "United States vs Mexico", [
                mk(f"KXWCGAME-{key}-USA", "United States", last_price=50),
                mk(f"KXWCGAME-{key}-MEX", "Mexico", last_price=24),
                mk(f"KXWCGAME-{key}-TIE", "Tie", last_price=26),
            ])],
            # Over 2.5 rich (mid 62) vs fair ~52 -> buy NO = low-scoring.
            "KXWCTOTAL": [ev("KXWCTOTAL", "United States vs Mexico: Total Goals", [
                mk(f"KXWCTOTAL-{key}-1", "Over 1.5 goals scored", last_price=75),
                mk(f"KXWCTOTAL-{key}-2", "Over 2.5 goals scored", yes_bid=60, yes_ask=64),
            ])],
            # Mexico over 1.5 rich (45) vs fair ~29 -> buy NO = low-scoring (correlated).
            "KXWCTEAMTOTAL": [ev("KXWCTEAMTOTAL", "United States vs Mexico: Team Total", [
                mk(f"KXWCTEAMTOTAL-{key}-MEX2", "Mexico over 1.5 goals", last_price=45),
            ])],
            "KXWCBTTS": [ev("KXWCBTTS", "United States vs Mexico: BTTS", [
                mk(f"KXWCBTTS-{key}-Y", "Both Teams To Score", last_price=53),
            ])],
            "KXWCCORNERS": [ev("KXWCCORNERS", "United States vs Mexico: Corners", [
                mk(f"KXWCCORNERS-{key}-1", "Over 9.5 corners", last_price=58),
            ])],
        }

    def list_events(self, *, series_ticker=None, status="open", with_markets=False):
        return list(self._by_series.get(series_ticker, [])) if status == "open" else []

    def get_event(self, event_ticker, *, with_markets=True):
        for evs in self._by_series.values():
            for e in evs:
                if e.event_ticker == event_ticker:
                    return e
        raise KeyError(event_ticker)

    def list_markets(self, *, event_ticker=None, status="open"):
        return []

    def get_market(self, ticker):
        raise KeyError(ticker)


class _DemoOdds(OddsDataClient):
    """Returns one fair-odds (vig-free) event so de-vig reproduces the targets:
    1X2 = .50/.27/.23, total 2.5 over .52."""

    def fetch_events(self, *, sport=WORLD_CUP_SPORT_KEY):
        return [
            OddsEvent(
                id="odds-usamex",
                home_team="United States",
                away_team="Mexico",
                commence_time=None,
                bookmakers=[
                    Bookmaker(
                        key="demobook",
                        markets=[
                            OddsMarket(
                                key="h2h",
                                outcomes=[
                                    OddsOutcome(name="United States", price=2.0),
                                    OddsOutcome(name="Mexico", price=1 / 0.23),
                                    OddsOutcome(name="Draw", price=1 / 0.27),
                                ],
                            ),
                            OddsMarket(
                                key="totals",
                                outcomes=[
                                    OddsOutcome(name="Over", price=1 / 0.52, point=2.5),
                                    OddsOutcome(name="Under", price=1 / 0.48, point=2.5),
                                ],
                            ),
                        ],
                    )
                ],
            )
        ]

    def fetch_reference_lines(self, *, sport=WORLD_CUP_SPORT_KEY):
        from .clients.odds import reference_lines_from_event

        return {e.id: reference_lines_from_event(e) for e in self.fetch_events(sport=sport)}


class _DemoNews(NewsClient):
    """Known news with Pulisic ruled out, to demonstrate prop suppression."""

    def get_team_news(self, team_id: str) -> TeamNews:
        if "united states" in team_id:
            return TeamNews(known=True, players_out=["Christian Pulisic"])
        return TeamNews(known=True)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def _emit(report, settings: Settings) -> None:
    md = render_markdown(report)
    print(md)
    # Shadow mode: persist the FULL report JSON (date-partitioned) so it can be
    # graded later, plus human-readable markdown + HTML alongside it.
    store = LocalReportStore(settings.output_dir)
    store.save_report(report)
    store.save_artifact(report.report_date, "report.md", md)
    store.save_artifact(report.report_date, "report.html", render_html(report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.run", description=__doc__)
    parser.add_argument("--date", help="match day to screen, YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--demo", action="store_true", help="run the full pipeline offline on synthetic clients"
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(json=settings.log_json)
    assert_read_only()  # hard-fail if any trade/order capability crept in

    target = date.fromisoformat(args.date) if args.date else date.today()

    if args.demo:
        report = build_daily_report(
            target, settings, venue="Demo",
            kalshi=_DemoKalshi(target), odds=_DemoOdds(), news=_DemoNews(),
        )
        _emit(report, settings)
        return 0

    # Live: discover from Polymarket (read-only public Gamma); fair value from odds.
    from .clients.polymarket import build_polymarket_client, discover_matches as poly_discover

    _kalshi, odds, news = _build_clients(settings, target.isoformat())
    discovered = poly_discover(build_polymarket_client())
    report = build_daily_report(
        target, settings, odds=odds, news=news, discovered=discovered, venue="Polymarket",
    )
    _emit(report, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
