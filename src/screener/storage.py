"""Persistence for daily reports and grading artifacts.

Shadow mode requires that every run's full output be saved, date-partitioned,
so it can be graded later. We define a small ``ReportStore`` interface and a
local-filesystem implementation now; an S3 implementation drops in behind the
same interface in the AWS deliverable (the date-partition layout is chosen to
map cleanly onto S3 key prefixes: ``date=YYYY-MM-DD/...``).

What's persisted is the whole :class:`DailyReport` as JSON — which carries the
inputs (reference lines), the lambdas, every fair value (flagged AND
below-threshold, needed for calibration), and the flagged edges. Round-trips via
pydantic so grading reloads an exact copy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import structlog

from .report import DailyReport

log = structlog.get_logger(__name__)

REPORT_JSON = "report.json"


class ReportStore(ABC):
    """Date-partitioned store for reports and derived artifacts."""

    @abstractmethod
    def save_report(self, report: DailyReport) -> str:
        """Persist the full report JSON. Returns a locator (path or S3 key)."""

    @abstractmethod
    def load_report(self, report_date: date) -> DailyReport:
        """Reload a previously saved report for grading."""

    @abstractmethod
    def save_artifact(self, report_date: date, name: str, content: str) -> str:
        """Persist a text artifact (e.g. report.md, grade.md) for the date."""


class LocalReportStore(ReportStore):
    """Filesystem store under ``<root>/date=YYYY-MM-DD/``."""

    def __init__(self, root: Path | str = "output") -> None:
        self.root = Path(root)

    def _dir(self, report_date: date) -> Path:
        return self.root / f"date={report_date.isoformat()}"

    def save_report(self, report: DailyReport) -> str:
        d = self._dir(report.report_date)
        d.mkdir(parents=True, exist_ok=True)
        path = d / REPORT_JSON
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        log.info("storage.saved_report", path=str(path))
        return str(path)

    def load_report(self, report_date: date) -> DailyReport:
        path = self._dir(report_date) / REPORT_JSON
        if not path.exists():
            raise FileNotFoundError(
                f"no persisted report at {path}; run the screener for {report_date} first"
            )
        return DailyReport.model_validate_json(path.read_text(encoding="utf-8"))

    def save_artifact(self, report_date: date, name: str, content: str) -> str:
        d = self._dir(report_date)
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(content, encoding="utf-8")
        log.info("storage.saved_artifact", path=str(path))
        return str(path)
