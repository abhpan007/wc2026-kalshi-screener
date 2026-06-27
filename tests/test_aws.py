"""Tests for the AWS integration using in-memory fakes (no boto3 / no AWS)."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from screener.aws.email import SesEmailer
from screener.aws.handler import run_screener
from screener.aws.s3_store import S3ReportStore
from screener.aws.secrets import load_secrets
from screener.config import Settings
from screener.models import Match, Team, XgStrategy
from screener.report import DailyReport, MatchReport
from screener.clients.news import StubNewsClient
from tests.conftest import FakeOdds, FakeSeriesKalshi, fair_odds_event, wc_match_events


# --------------------------------------------------------------------------- #
# Fake AWS clients
# --------------------------------------------------------------------------- #
class _FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, *, Bucket, Key):
        class _Body:
            def __init__(self, b): self._b = b
            def read(self): return self._b
        return {"Body": _Body(self.objects[Key])}


class _FakeSes:
    def __init__(self):
        self.sent: list[dict] = []

    def send_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "msg-123"}


class _FakeSecrets:
    def __init__(self, payload):
        self._payload = payload

    def get_secret_value(self, *, SecretId):
        return {"SecretString": json.dumps(self._payload)}


# --------------------------------------------------------------------------- #
# S3 store
# --------------------------------------------------------------------------- #
def _report() -> DailyReport:
    match = Match(
        match_id="m1",
        home=Team(team_id="usa", name="United States"),
        away=Team(team_id="mex", name="Mexico"),
        kickoff_utc=datetime(2026, 6, 21, 22, 0, tzinfo=ZoneInfo("UTC")),
    )
    return DailyReport(
        report_date=date(2026, 6, 21), strategy=XgStrategy.BOOK_ANCHORED, threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=[MatchReport(match=match)],
    )


def test_s3_store_roundtrip_and_key_layout():
    s3 = _FakeS3()
    store = S3ReportStore("my-bucket", client=s3, prefix="screener")
    locator = store.save_report(_report())
    assert locator == "s3://my-bucket/screener/date=2026-06-21/report.json"
    assert "screener/date=2026-06-21/report.json" in s3.objects
    loaded = store.load_report(date(2026, 6, 21))
    assert loaded.matches[0].match.away.name == "Mexico"


def test_s3_store_artifact_content_type():
    s3 = _FakeS3()
    store = S3ReportStore("b", client=s3)
    store.save_artifact(date(2026, 6, 21), "report.html", "<h1>hi</h1>")
    assert "date=2026-06-21/report.html" in s3.objects


# --------------------------------------------------------------------------- #
# SES
# --------------------------------------------------------------------------- #
def test_ses_send_shape():
    ses = _FakeSes()
    emailer = SesEmailer(client=ses, sender="me@x.com", recipients=["you@x.com"])
    mid = emailer.send(subject="S", html_body="<b>h</b>", text_body="h")
    assert mid == "msg-123"
    call = ses.sent[0]
    assert call["Source"] == "me@x.com"
    assert call["Destination"]["ToAddresses"] == ["you@x.com"]
    assert call["Message"]["Body"]["Html"]["Data"] == "<b>h</b>"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def test_load_secrets_parses_json():
    out = load_secrets("sid", client=_FakeSecrets({"ODDS_API_KEY": "k1"}))
    assert out == {"ODDS_API_KEY": "k1"}


def test_load_secrets_degrades_on_failure():
    class _Boom:
        def get_secret_value(self, *, SecretId):
            raise RuntimeError("no access")
    assert load_secrets("sid", client=_Boom()) == {}


# --------------------------------------------------------------------------- #
# run_screener core (the handler's testable heart)
# --------------------------------------------------------------------------- #
def test_run_screener_persists_and_emails():
    s3 = _FakeS3()
    ses = _FakeSes()
    store = S3ReportStore("b", client=s3)
    emailer = SesEmailer(client=ses, sender="me@x.com", recipients=["you@x.com"])

    report = run_screener(
        date(2026, 6, 21),
        Settings(),
        kalshi=FakeSeriesKalshi(wc_match_events()),
        odds=FakeOdds([fair_odds_event()]),
        news=StubNewsClient(),
        store=store,
        emailer=emailer,
    )
    # Persisted report.json + md + html.
    keys = set(s3.objects)
    assert any(k.endswith("report.json") for k in keys)
    assert any(k.endswith("report.md") for k in keys)
    assert any(k.endswith("report.html") for k in keys)
    # Emailed once, subject reflects edges.
    assert len(ses.sent) == 1
    assert "edge" in ses.sent[0]["Message"]["Subject"]["Data"]
    assert report.total_flagged >= 1


def test_run_screener_email_failure_is_nonfatal():
    class _BoomSes:
        def send_email(self, **kw):
            raise RuntimeError("SES not verified")

    s3 = _FakeS3()
    store = S3ReportStore("b", client=s3)
    emailer = SesEmailer(client=_BoomSes(), sender="me@x.com", recipients=["you@x.com"])
    # Must not raise — the report is already persisted to S3.
    report = run_screener(
        date(2026, 6, 21), Settings(),
        kalshi=FakeSeriesKalshi(wc_match_events()), odds=FakeOdds([fair_odds_event()]),
        news=StubNewsClient(), store=store, emailer=emailer,
    )
    assert any(k.endswith("report.json") for k in s3.objects)
    assert report is not None


def test_run_screener_without_emailer_skips_email():
    s3 = _FakeS3()
    store = S3ReportStore("b", client=s3)
    run_screener(
        date(2026, 6, 21), Settings(),
        kalshi=FakeSeriesKalshi(wc_match_events()), odds=FakeOdds([fair_odds_event()]),
        news=StubNewsClient(), store=store, emailer=None,
    )
    assert any(k.endswith("report.json") for k in s3.objects)
