#!/usr/bin/env python3
"""CDK app entrypoint.

Configure via CDK context (``cdk deploy -c email_sender=... -c email_recipients=...``)
or environment variables. The sender must be an SES-verified identity; in the
SES sandbox the recipients must be verified too.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from screener_stack import ScreenerStack

app = cdk.App()


def _ctx(key: str, default: str = "") -> str:
    return app.node.try_get_context(key) or os.environ.get(key.upper(), default)


ScreenerStack(
    app,
    "Wc2026ScreenerStack",
    email_sender=_ctx("email_sender"),
    email_recipients=_ctx("email_recipients"),
    kalshi_series=_ctx("kalshi_series"),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

app.synth()
