"""SES email delivery for the daily report.

Sending email is one of the three side effects the design permits (read APIs,
write S3, send email). The SES client is injected; tests pass a fake capturing
``send_email``.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


class SesEmailer:
    def __init__(self, *, client: Any, sender: str, recipients: list[str]) -> None:
        self.client = client
        self.sender = sender
        self.recipients = recipients

    def send(self, *, subject: str, html_body: str, text_body: Optional[str] = None) -> str:
        body: dict[str, Any] = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
        if text_body is not None:
            body["Text"] = {"Data": text_body, "Charset": "UTF-8"}
        resp = self.client.send_email(
            Source=self.sender,
            Destination={"ToAddresses": self.recipients},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
        )
        message_id = resp.get("MessageId", "") if isinstance(resp, dict) else ""
        log.info("ses.sent", recipients=self.recipients, message_id=message_id)
        return message_id
