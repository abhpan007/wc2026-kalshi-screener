"""S3-backed :class:`ReportStore`.

Same interface as :class:`screener.storage.LocalReportStore`, so the pipeline
and grader don't care which is in use. The key layout mirrors the local layout
(``[prefix/]date=YYYY-MM-DD/<name>``) so S3 prefixes partition by match-day.

The boto3 S3 client is injected; tests pass a fake with ``put_object`` /
``get_object``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog

from ..report import DailyReport
from ..storage import REPORT_JSON, ReportStore

log = structlog.get_logger(__name__)


def _content_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".html"):
        return "text/html"
    return "text/markdown"


class S3ReportStore(ReportStore):
    def __init__(self, bucket: str, *, client: Any, prefix: str = "") -> None:
        self.bucket = bucket
        self.client = client
        self.prefix = prefix.strip("/")

    def _key(self, report_date: date, name: str) -> str:
        base = f"date={report_date.isoformat()}/{name}"
        return f"{self.prefix}/{base}" if self.prefix else base

    def save_report(self, report: DailyReport) -> str:
        key = self._key(report.report_date, REPORT_JSON)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=report.model_dump_json(indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        log.info("s3.saved_report", bucket=self.bucket, key=key)
        return f"s3://{self.bucket}/{key}"

    def load_report(self, report_date: date) -> DailyReport:
        key = self._key(report_date, REPORT_JSON)
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return DailyReport.model_validate_json(obj["Body"].read())

    def save_artifact(self, report_date: date, name: str, content: str) -> str:
        key = self._key(report_date, name)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=_content_type(name),
        )
        log.info("s3.saved_artifact", bucket=self.bucket, key=key)
        return f"s3://{self.bucket}/{key}"
