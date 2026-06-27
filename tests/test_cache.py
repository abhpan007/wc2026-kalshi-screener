"""Tests for the per-match-day disk cache."""

from __future__ import annotations

from pathlib import Path

from screener.clients.cache import DiskCache, NullCache


def test_set_get_roundtrip(tmp_path: Path):
    c = DiskCache(tmp_path, namespace="2026-06-21", source="kalshi")
    c.set("a?x=1", {"hello": "world"})
    assert c.get("a?x=1") == {"hello": "world"}


def test_miss_returns_none(tmp_path: Path):
    c = DiskCache(tmp_path, namespace="2026-06-21", source="kalshi")
    assert c.get("nope") is None


def test_namespace_and_source_partition(tmp_path: Path):
    a = DiskCache(tmp_path, namespace="2026-06-21", source="kalshi")
    b = DiskCache(tmp_path, namespace="2026-06-22", source="kalshi")
    d = DiskCache(tmp_path, namespace="2026-06-21", source="odds")
    a.set("k", 1)
    assert b.get("k") is None  # different day
    assert d.get("k") is None  # different source
    assert a.get("k") == 1


def test_corrupt_entry_is_a_miss(tmp_path: Path):
    c = DiskCache(tmp_path, namespace="d", source="s")
    c.set("k", {"v": 1})
    # Corrupt the underlying file.
    f = next((tmp_path / "d" / "s").glob("*.json"))
    f.write_text("{not json", encoding="utf-8")
    assert c.get("k") is None


def test_human_readable_key_stored(tmp_path: Path):
    c = DiskCache(tmp_path, namespace="d", source="s")
    c.set("/markets?status=open", {"v": 1})
    f = next((tmp_path / "d" / "s").glob("*.json"))
    assert "/markets?status=open" in f.read_text(encoding="utf-8")


def test_null_cache_stores_nothing():
    c = NullCache()
    c.set("k", {"v": 1})
    assert c.get("k") is None
