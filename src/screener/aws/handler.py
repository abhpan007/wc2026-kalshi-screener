"""Lambda entrypoint.

``run_screener`` is the pure-ish core: given clients + a store (+ optional
emailer), it runs the pipeline, persists the report, and emails it. It is fully
testable with fakes. ``lambda_handler`` is the thin AWS wiring around it — it
imports boto3 lazily, loads secrets, builds the real clients/store/emailer, and
calls the core. The local CLI (``screener.run``) is the same pipeline with a
local store and no email.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional

import structlog

from ..config import Settings
from ..guardrails import assert_read_only
from ..logging import configure_logging
from ..models import CENTRAL
from ..pipeline import build_daily_report
from ..report import DailyReport, render_html, render_markdown
from ..storage import ReportStore

log = structlog.get_logger(__name__)


def run_screener(
    target: date,
    settings: Settings,
    *,
    kalshi: Any,
    odds: Any,
    news: Any,
    store: ReportStore,
    emailer: Optional[Any] = None,
    kalshi_series: Optional[list[str]] = None,
) -> DailyReport:
    """Run the pipeline, persist outputs, and (if configured) email the report."""
    report = build_daily_report(
        target,
        settings,
        kalshi=kalshi,
        odds=odds,
        news=news,
        kalshi_series=kalshi_series,
    )
    md = render_markdown(report)
    html = render_html(report)

    store.save_report(report)
    store.save_artifact(target, "report.md", md)
    store.save_artifact(target, "report.html", html)

    if emailer is not None:
        flagged = report.total_flagged
        subject = (
            f"WC Screener {target.isoformat()} — "
            + (f"{flagged} edge(s)" if flagged else "thin board, no edges")
        )
        # Email is the last, least-critical step: the report is already safe in
        # S3. A delivery failure (e.g. SES not yet verified) must NOT fail the
        # run — log it and move on, consistent with the pipeline's degrade-don't-
        # abort philosophy.
        try:
            emailer.send(subject=subject, html_body=html, text_body=md)
        except Exception as exc:
            log.warning("email.send_failed", error=str(exc))

    return report


def lambda_handler(event: dict, context: object) -> dict:  # pragma: no cover - AWS wiring
    """EventBridge-triggered entrypoint. Reads config from env, secrets from
    Secrets Manager, and the three permitted side effects only (read APIs, write
    S3, send email)."""
    import boto3

    from ..run import _build_clients  # reuse the same client construction as local
    from .email import SesEmailer
    from .s3_store import S3ReportStore
    from .secrets import load_secrets

    configure_logging(json=True)
    assert_read_only()  # hard-fail if any trade/order capability crept in

    # Secrets -> env, so the existing env-based client construction picks them up.
    secret_id = os.environ.get("SCREENER_SECRET_ID")
    if secret_id:
        secrets = load_secrets(secret_id, client=boto3.client("secretsmanager"))
        if "ODDS_API_KEY" in secrets:
            os.environ["SCREENER_ODDS_API_KEY"] = secrets["ODDS_API_KEY"]
        if "NEWS_API_KEY" in secrets:
            os.environ["SCREENER_NEWS_API_KEY"] = secrets["NEWS_API_KEY"]

    settings = Settings.from_env()
    # Default to today's America/Chicago match-day; allow an explicit override via
    # the event payload ({"date": "YYYY-MM-DD"}) for manual re-runs / backfills.
    target = datetime.now(tz=CENTRAL).date()
    if isinstance(event, dict) and event.get("date"):
        try:
            target = date.fromisoformat(event["date"])
        except ValueError:
            log.warning("handler.bad_date_override", value=event.get("date"))
    kalshi, odds, news = _build_clients(settings, target.isoformat())

    store = S3ReportStore(os.environ["SCREENER_S3_BUCKET"], client=boto3.client("s3"))
    emailer = None
    sender = os.environ.get("SCREENER_EMAIL_SENDER")
    recipients = [r for r in os.environ.get("SCREENER_EMAIL_RECIPIENTS", "").split(",") if r]
    if sender and recipients:
        emailer = SesEmailer(client=boto3.client("ses"), sender=sender, recipients=recipients)

    series = [t for t in os.environ.get("SCREENER_KALSHI_SERIES", "").split(",") if t] or None
    report = run_screener(
        target, settings,
        kalshi=kalshi, odds=odds, news=news,
        store=store, emailer=emailer, kalshi_series=series,
    )
    return {"date": target.isoformat(), "matches": len(report.matches), "flagged": report.total_flagged}
