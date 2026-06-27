"""Tests for the shared HTTP client: caching, retries, error handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from screener.clients.cache import DiskCache
from screener.clients.http import HttpClient, TransientHttpError
from tests.conftest import FakeResponse, FakeSession


def _client(handler, tmp_path=None, **kw):
    cache = DiskCache(tmp_path, "d", "s") if tmp_path else None
    session = FakeSession(handler)
    # No real backoff waiting in tests.
    return (
        HttpClient(
            "https://api.test/v2",
            session=session,
            cache=cache,
            backoff_multiplier=0.0,
            backoff_max=0.0,
            **kw,
        ),
        session,
    )


def test_get_json_returns_parsed_body():
    client, _ = _client(lambda url, params: FakeResponse(200, {"ok": True}))
    assert client.get_json("/markets") == {"ok": True}


def test_cache_prevents_second_fetch(tmp_path: Path):
    client, session = _client(lambda u, p: FakeResponse(200, {"n": 1}), tmp_path)
    client.get_json("/markets", {"status": "open"})
    client.get_json("/markets", {"status": "open"})
    assert len(session.calls) == 1  # second call served from cache


def test_force_refresh_bypasses_cache(tmp_path: Path):
    client, session = _client(lambda u, p: FakeResponse(200, {"n": 1}), tmp_path)
    client.get_json("/markets")
    client.get_json("/markets", force_refresh=True)
    assert len(session.calls) == 2


def test_params_differentiate_cache_keys(tmp_path: Path):
    client, session = _client(lambda u, p: FakeResponse(200, dict(p or {})), tmp_path)
    client.get_json("/markets", {"status": "open"})
    client.get_json("/markets", {"status": "settled"})
    assert len(session.calls) == 2


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(url, params):
        calls["n"] += 1
        return FakeResponse(500) if calls["n"] < 3 else FakeResponse(200, {"ok": 1})

    client, _ = _client(handler, max_attempts=5)
    assert client.get_json("/x") == {"ok": 1}
    assert calls["n"] == 3


def test_gives_up_after_max_attempts():
    client, session = _client(lambda u, p: FakeResponse(503), max_attempts=3)
    with pytest.raises(TransientHttpError):
        client.get_json("/x")
    assert len(session.calls) == 3


def test_4xx_is_not_retried():
    client, session = _client(lambda u, p: FakeResponse(404), max_attempts=4)
    with pytest.raises(RuntimeError, match="404"):
        client.get_json("/missing")
    assert len(session.calls) == 1  # client error, no retry


def test_network_exception_is_retried():
    calls = {"n": 0}

    def handler(url, params):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("boom")
        return FakeResponse(200, {"ok": 1})

    client, _ = _client(handler, max_attempts=4)
    assert client.get_json("/x") == {"ok": 1}
    assert calls["n"] == 2
