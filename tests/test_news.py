"""Tests for the read-only NewsClient: stub + HTTP skeleton.

Covers the spec's required behaviors:
  - the stub ALWAYS returns ``known=False`` and never raises;
  - the unknown (``known=False``) vs. known-and-clean (``known=True``, empty
    lists) distinction;
  - the HTTP client degrades to ``known=False`` on 500/network errors;
  - the HTTP client parses a fixture into players_out / players_doubtful.

No test touches the network (FakeSession only).
"""

from __future__ import annotations

import pytest

from screener.clients.http import HttpClient
from screener.clients.news import (
    HttpNewsClient,
    NewsClient,
    StubNewsClient,
)
from tests.conftest import FakeResponse, FakeSession, load_fixture


def _http(handler, **kw) -> HttpClient:
    session = FakeSession(handler)
    # No real backoff waiting in tests.
    return HttpClient(
        "https://api.test/v1",
        session=session,
        backoff_multiplier=0.0,
        backoff_max=0.0,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Stub
# --------------------------------------------------------------------------- #
def test_stub_implements_interface():
    assert isinstance(StubNewsClient(), NewsClient)


def test_stub_always_unknown():
    news = StubNewsClient().get_team_news("USA")
    assert news.known is False
    assert news.players_out == []
    assert news.players_doubtful == []


def test_stub_match_news_both_unknown_never_raises():
    home, away = StubNewsClient().get_match_news("USA", "MEX")
    assert home.known is False and away.known is False


def test_unknown_is_distinct_from_known_and_clean():
    # The load-bearing distinction: "we don't know" != "nobody is out".
    unknown = StubNewsClient().get_team_news("USA")
    assert unknown.known is False

    http = _http(lambda u, p: FakeResponse(200, load_fixture("news_injuries_empty.json")))
    known_clean = HttpNewsClient(http, api_key="k").get_team_news("MEX")
    assert known_clean.known is True
    assert known_clean.players_out == []
    assert known_clean.players_doubtful == []


# --------------------------------------------------------------------------- #
# HTTP skeleton
# --------------------------------------------------------------------------- #
def test_http_implements_interface():
    http = _http(lambda u, p: FakeResponse(200, {"response": []}))
    assert isinstance(HttpNewsClient(http, api_key="k"), NewsClient)


def test_http_parses_fixture_into_out_and_doubtful():
    http = _http(lambda u, p: FakeResponse(200, load_fixture("news_injuries_usa.json")))
    news = HttpNewsClient(http, api_key="k").get_team_news("USA")
    assert news.known is True
    assert news.players_out == ["Sergino Dest"]
    assert news.players_doubtful == ["Tyler Adams"]


def test_http_degrades_to_unknown_on_500():
    # 5xx is retried then exhausted; the client must catch and return unknown.
    http = _http(lambda u, p: FakeResponse(500), max_attempts=2)
    news = HttpNewsClient(http, api_key="k").get_team_news("USA")
    assert news.known is False


def test_http_degrades_to_unknown_on_network_error():
    def boom(url, params):
        raise ConnectionError("boom")

    http = _http(boom, max_attempts=2)
    news = HttpNewsClient(http, api_key="k").get_team_news("USA")
    assert news.known is False


def test_http_degrades_to_unknown_on_4xx():
    http = _http(lambda u, p: FakeResponse(404))
    news = HttpNewsClient(http, api_key="k").get_team_news("USA")
    assert news.known is False


def test_http_degrades_to_unknown_on_malformed_payload():
    http = _http(lambda u, p: FakeResponse(200, ["not", "a", "dict"]))
    news = HttpNewsClient(http, api_key="k").get_team_news("USA")
    assert news.known is False


def test_http_without_api_key_is_unknown(monkeypatch):
    # No key configured -> we genuinely cannot know; never raise.
    monkeypatch.delenv("SCREENER_NEWS_API_KEY", raising=False)
    http = _http(lambda u, p: FakeResponse(200, load_fixture("news_injuries_usa.json")))
    news = HttpNewsClient(http).get_team_news("USA")
    assert news.known is False


def test_http_reads_api_key_from_env(monkeypatch):
    monkeypatch.setenv("SCREENER_NEWS_API_KEY", "env-key")
    http = _http(lambda u, p: FakeResponse(200, load_fixture("news_injuries_empty.json")))
    news = HttpNewsClient(http).get_team_news("MEX")
    assert news.known is True


def test_http_match_news_returns_both_sides():
    def route(url, params):
        if (params or {}).get("team") == "USA":
            return FakeResponse(200, load_fixture("news_injuries_usa.json"))
        return FakeResponse(200, load_fixture("news_injuries_empty.json"))

    http = _http(route)
    home, away = HttpNewsClient(http, api_key="k").get_match_news("USA", "MEX")
    assert home.players_out == ["Sergino Dest"]
    assert away.known is True and away.players_out == []


def test_news_client_has_no_order_methods():
    # Defense in depth alongside the source scanner.
    forbidden = {"create_order", "place_order", "cancel_order", "submit_order"}
    assert forbidden.isdisjoint(dir(HttpNewsClient))
    assert forbidden.isdisjoint(dir(StubNewsClient))
