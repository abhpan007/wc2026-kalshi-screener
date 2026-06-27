"""Read-only / no-trade guardrails, enforced in code (not just docs).

This project is a screener. It must NEVER place, size, or execute a trade, and
must never call an order/portfolio-mutating endpoint. We enforce that two ways:

1. :func:`assert_read_only` scans the package source for forbidden identifiers
   (order/trade/portfolio-write verbs) and raises if any appear. ``run.py``
   calls this at startup; a unit test calls it too, so a regression fails CI.
2. The only outbound side effects the design permits are: reading APIs, writing
   to S3, and sending email. Any other network *write* is a guardrail breach;
   the test suite asserts the forbidden patterns stay absent.

The scan is deliberately blunt (substring match on identifiers). False positives
are cheap to resolve by renaming; a missed real ordering call is not.
"""

from __future__ import annotations

import re
from pathlib import Path

# Identifiers that would indicate trade/order/portfolio-write capability. Matched
# against the source as whole-ish tokens. Keep this list conservative but broad.
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "create_order",
    "place_order",
    "submit_order",
    "cancel_order",
    "create_trade",
    "execute_trade",
    "place_trade",
    "amend_order",
    "batch_create_orders",
    "batch_cancel_orders",
    "decrease_order",
    "create_position",
    "portfolio_write",
)

# This module's own definition of the list must not trip the scan, so we skip it.
_SELF = Path(__file__).name


def _iter_source_files(root: Path):
    for path in root.rglob("*.py"):
        if path.name == _SELF:
            continue
        yield path


def scan_for_violations(package_root: Path | None = None) -> dict[str, list[str]]:
    """Return ``{file: [patterns found]}`` for any forbidden identifier.

    Empty dict means clean. Matches on a word boundary so ``reorder`` etc. do
    not false-trigger on ``order``-suffixed unrelated words.
    """
    root = package_root or Path(__file__).resolve().parent
    violations: dict[str, list[str]] = {}
    regexes = {p: re.compile(rf"\b{re.escape(p)}\b") for p in FORBIDDEN_PATTERNS}
    for path in _iter_source_files(root):
        text = path.read_text(encoding="utf-8")
        hits = [p for p, rx in regexes.items() if rx.search(text)]
        if hits:
            violations[str(path)] = hits
    return violations


def assert_read_only(package_root: Path | None = None) -> None:
    """Raise if any order/trade/portfolio-write identifier exists in the package."""
    violations = scan_for_violations(package_root)
    if violations:
        detail = "; ".join(f"{f}: {', '.join(p)}" for f, p in violations.items())
        raise RuntimeError(
            "GUARDRAIL VIOLATION: order/trade capability detected in source — "
            "this project is read-only and must never place trades. " + detail
        )
