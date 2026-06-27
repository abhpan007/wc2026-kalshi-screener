"""Tests for the read-only KalshiHttpClient against recorded fixtures."""

from __future__ import annotations

import pytest

from screener.clients.http import HttpClient
from screener.clients.kalshi import KalshiHttpClient, KalshiMarket, MarketDataClient
from tests.conftest import FakeResponse, FakeSession, load_fixture


def _route(url: str, params):
    """Map fixture data onto Kalshi paths, honoring the events cursor pagination."""
    if url.endswith("/events"):
        if (params or {}).get("cursor") == "PAGE2":
            return FakeResponse(200, load_fixture("kalshi_events_page2.json"))
        return FakeResponse(200, load_fixture("kalshi_events_page1.json"))
    if "/events/KXWC2026-USAMEX" in url:
        return FakeResponse(200, load_fixture("kalshi_event_usamex.json"))
    return FakeResponse(404)


def _client() -> tuple[KalshiHttpClient, FakeSession]:
    session = FakeSession(_route)
    http = HttpClient("https://api.test/trade-api/v2", session=session)
    return KalshiHttpClient(http), session


def test_client_implements_interface():
    client, _ = _client()
    assert isinstance(client, MarketDataClient)


def test_list_events_paginates_via_cursor():
    client, session = _client()
    events = client.list_events()
    tickers = {e.event_ticker for e in events}
    assert tickers == {"KXWC2026-USAMEX", "KXNBA-LALBOS", "KXWC2026-BRAARG"}
    # Two pages fetched (page1 had cursor PAGE2).
    assert len(session.calls) == 2


def test_get_event_unwraps_and_parses_markets():
    client, _ = _client()
    event = client.get_event("KXWC2026-USAMEX")
    assert event.series_ticker == "KXWC2026"
    assert len(event.markets) == 13
    assert all(isinstance(m, KalshiMarket) for m in event.markets)


def test_yes_price_prefers_bid_ask_midpoint():
    m = KalshiMarket(ticker="x", yes_bid=50, yes_ask=54, last_price=99)
    assert m.yes_price_cents() == 52  # midpoint, not last_price


def test_yes_price_falls_back_to_last_price():
    m = KalshiMarket(ticker="x", last_price=40)
    assert m.yes_price_cents() == 40


def test_yes_price_none_when_no_data():
    assert KalshiMarket(ticker="x").yes_price_cents() is None


def test_unknown_fields_are_ignored():
    # Schema drift must not break parsing.
    m = KalshiMarket.model_validate({"ticker": "x", "some_new_field": 123})
    assert m.ticker == "x"


def test_client_has_no_order_methods():
    # Defense in depth alongside the source scanner: the interface surface must
    # not expose anything that could place/modify an order.
    forbidden = {"create_order", "place_order", "cancel_order", "submit_order"}
    assert forbidden.isdisjoint(dir(KalshiHttpClient))
