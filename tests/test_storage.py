"""Tests for the local report store (round-trips, artifacts, missing reports)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from screener.models import Match, Team, XgStrategy
from screener.report import DailyReport, MatchReport
from screener.storage import LocalReportStore


def _report() -> DailyReport:
    match = Match(
        match_id="m1",
        home=Team(team_id="usa", name="United States"),
        away=Team(team_id="mex", name="Mexico"),
        kickoff_utc=datetime(2026, 6, 21, 22, 0, tzinfo=ZoneInfo("UTC")),
    )
    return DailyReport(
        report_date=date(2026, 6, 21),
        strategy=XgStrategy.BOOK_ANCHORED,
        threshold_cents=3,
        generated_at=datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        matches=[MatchReport(match=match)],
    )


def test_save_and_load_roundtrip(tmp_path: Path):
    store = LocalReportStore(tmp_path)
    store.save_report(_report())
    loaded = store.load_report(date(2026, 6, 21))
    assert loaded.report_date == date(2026, 6, 21)
    assert loaded.matches[0].match.home.name == "United States"


def test_date_partition_layout(tmp_path: Path):
    store = LocalReportStore(tmp_path)
    store.save_report(_report())
    assert (tmp_path / "date=2026-06-21" / "report.json").exists()


def test_save_artifact(tmp_path: Path):
    store = LocalReportStore(tmp_path)
    store.save_artifact(date(2026, 6, 21), "grade.md", "# hi")
    assert (tmp_path / "date=2026-06-21" / "grade.md").read_text() == "# hi"


def test_load_missing_raises(tmp_path: Path):
    store = LocalReportStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_report(date(2026, 6, 21))
