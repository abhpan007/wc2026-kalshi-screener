"""Kalshi read-only market-data client.

Uses ONLY public read endpoints under the trade-api v2. Market-data endpoints do
not require authentication; if Kalshi ever gates them, an API key can be supplied
via headers, but NO order/portfolio scope is ever requested or used.

Endpoints used (documented per the spec; see README "Kalshi API"):
    GET /events                      list events (filter by series_ticker/status)
    GET /events/{event_ticker}       single event (with nested markets optional)
    GET /markets                     list markets (filter by event_ticker/status)
    GET /markets/{ticker}            single market
    GET /markets/{ticker}/orderbook  market orderbook (read)

Base URL (production): https://api.elections.kalshi.com/trade-api/v2

GUARDRAIL: this module defines read methods only. There is deliberately no
create/place/cancel order or any portfolio-mutating method anywhere. The source
scanner in ``screener.guardrails`` enforces this across the package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field

from .http import HttpClient

log = structlog.get_logger(__name__)

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Kalshi caps page size at 100 for list endpoints; we page with the cursor.
PAGE_LIMIT = 100
MAX_PAGES = 50  # hard stop so a bad cursor loop can't run forever


# --------------------------------------------------------------------------- #
# Response models (extra fields ignored so schema drift doesn't break a run)
# --------------------------------------------------------------------------- #
class KalshiMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    event_ticker: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    yes_sub_title: Optional[str] = None
    status: Optional[str] = None
    # Prices are integer cents (0-100). bid/ask are best book; last_price is last trade.
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    expiration_time: Optional[str] = None

    def yes_price_cents(self) -> Optional[int]:
        """Best single read of the market's Yes price, in cents.

        Prefer the bid/ask midpoint when both sides are quoted (most current);
        fall back to last traded price; else None. We screen against this number.
        """
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask > 0:
            return round((self.yes_bid + self.yes_ask) / 2)
        if self.last_price is not None and self.last_price > 0:
            return self.last_price
        return None


class KalshiEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_ticker: str
    series_ticker: Optional[str] = None
    title: Optional[str] = None
    sub_title: Optional[str] = None
    markets: list[KalshiMarket] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Interface (read-only). Swap providers by implementing this ABC.
# --------------------------------------------------------------------------- #
class MarketDataClient(ABC):
    """Read-only prediction-market data source. NO write/order methods, ever."""

    @abstractmethod
    def list_events(
        self,
        *,
        series_ticker: Optional[str] = None,
        status: str = "open",
        with_markets: bool = False,
    ) -> list[KalshiEvent]: ...

    @abstractmethod
    def get_event(self, event_ticker: str, *, with_markets: bool = True) -> KalshiEvent: ...

    @abstractmethod
    def list_markets(
        self, *, event_ticker: Optional[str] = None, status: str = "open"
    ) -> list[KalshiMarket]: ...

    @abstractmethod
    def get_market(self, ticker: str) -> KalshiMarket: ...


# --------------------------------------------------------------------------- #
# Concrete HTTP client
# --------------------------------------------------------------------------- #
class KalshiHttpClient(MarketDataClient):
    """Concrete read-only Kalshi client built on the shared :class:`HttpClient`."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    # -- paging helper ----------------------------------------------------- #
    def _paged(self, path: str, container: str, params: dict) -> list[dict]:
        """Follow Kalshi's cursor pagination, collecting ``container`` items."""
        items: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            page_params = dict(params, limit=PAGE_LIMIT)
            if cursor:
                page_params["cursor"] = cursor
            data = self._http.get_json(path, page_params)
            items.extend(data.get(container, []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return items

    # -- interface --------------------------------------------------------- #
    def list_events(
        self,
        *,
        series_ticker: Optional[str] = None,
        status: str = "open",
        with_markets: bool = False,
    ) -> list[KalshiEvent]:
        params: dict = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_markets:
            # The /events list endpoint returns each event's markets nested when
            # asked — lets us pull a whole series' matches+markets in one paging.
            params["with_nested_markets"] = "true"
        raw = self._paged("/events", "events", params)
        return [KalshiEvent.model_validate(e) for e in raw]

    def get_event(self, event_ticker: str, *, with_markets: bool = True) -> KalshiEvent:
        params = {"with_nested_markets": "true"} if with_markets else None
        data = self._http.get_json(f"/events/{event_ticker}", params)
        # Kalshi wraps the event under an "event" key.
        return KalshiEvent.model_validate(data.get("event", data))

    def list_markets(
        self, *, event_ticker: Optional[str] = None, status: str = "open"
    ) -> list[KalshiMarket]:
        params: dict = {"status": status}
        if event_ticker:
            params["event_ticker"] = event_ticker
        raw = self._paged("/markets", "markets", params)
        return [KalshiMarket.model_validate(m) for m in raw]

    def get_market(self, ticker: str) -> KalshiMarket:
        data = self._http.get_json(f"/markets/{ticker}")
        return KalshiMarket.model_validate(data.get("market", data))
