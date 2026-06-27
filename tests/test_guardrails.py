"""Guardrail tests: the package must contain no trade/order capability, and the
scanner must actually catch one if introduced."""

from __future__ import annotations

from pathlib import Path

import pytest

from screener.guardrails import assert_read_only, scan_for_violations


def test_package_is_clean():
    assert scan_for_violations() == {}


def test_assert_read_only_passes_on_current_source():
    assert_read_only()  # must not raise


def test_scanner_detects_an_injected_violation(tmp_path: Path):
    bad = tmp_path / "sneaky.py"
    bad.write_text("def create_order():\n    return 'oops'\n")
    violations = scan_for_violations(tmp_path)
    assert str(bad) in violations
    assert "create_order" in violations[str(bad)]


def test_assert_read_only_raises_on_violation(tmp_path: Path):
    (tmp_path / "trade.py").write_text("def place_order(): pass\n")
    with pytest.raises(RuntimeError, match="GUARDRAIL VIOLATION"):
        assert_read_only(tmp_path)


def test_word_boundary_avoids_false_positives(tmp_path: Path):
    # "reorder_list" should NOT trip the create_order/place_order patterns.
    (tmp_path / "ok.py").write_text("def reorder_list(): pass\ndef preorder(): pass\n")
    assert scan_for_violations(tmp_path) == {}
