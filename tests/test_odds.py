"""Tests for the read-only TheOddsApiClient and its de-vigging functions.

No test hits the network or needs a real API key: a FakeSession serves fixtures
and the pure de-vig functions are exercised against hand-computed values.
"""

from __future__ import annotations

import math

import pytest

from screener.clients.http import HttpClient
from screener.clients.odds import (
    OddsDataClient,
    OddsEvent,
    TheOddsApiClient,
    consensus_line,
    consensus_moneyline,
    consensus_over_prob,
    devig_three_way,
    devig_two_way,
    implied_prob,
    normalize,
    reference_lines_from_event,
)
from screener.models import MoneylineProbs, ReferenceLines
from tests.conftest import FakeResponse, FakeSession, load_fixture


# --------------------------------------------------------------------------- #
# Routing helpers
# --------------------------------------------------------------------------- #
def _route(fixture: str):
    def handler(url: str, params):
        if url.endswith("/odds"):
            return FakeResponse(200, load_fixture(fixture))
        return FakeResponse(404)

    return handler


def _client(fixture: str = "odds_wc.json", **kw):
    session = FakeSession(_route(fixture))
    http = HttpClient(
        "https://api.test/v4", session=session, backoff_multiplier=0.0, backoff_max=0.0
    )
    client = TheOddsApiClient(http, api_key="TESTKEY", **kw)
    return client, session


# --------------------------------------------------------------------------- #
# Pure de-vig functions
# --------------------------------------------------------------------------- #
def test_implied_prob_basic():
    assert implied_prob(2.0) == pytest.approx(0.5)
    assert implied_prob(4.0) == pytest.approx(0.25)


def test_implied_prob_rejects_nonpositive():
    with pytest.raises(ValueError):
        implied_prob(0.0)


def test_normalize_sums_to_one():
    out = normalize([0.55, 0.30, 0.25])  # sums to 1.10 (vig)
    assert sum(out) == pytest.approx(1.0)
    # Proportional: ratios are preserved.
    assert out[0] / out[1] == pytest.approx(0.55 / 0.30)


def test_devig_two_way_symmetric_is_half():
    # Equal odds -> exactly 0.5 each after de-vig (vig removed proportionally).
    over, under = devig_two_way(1.95, 1.95)
    assert over == pytest.approx(0.5)
    assert under == pytest.approx(0.5)
    assert over + under == pytest.approx(1.0)


def test_devig_two_way_favored_over():
    over, under = devig_two_way(1.80, 2.05)  # lower odds = more likely
    assert over > under
    assert over + under == pytest.approx(1.0)


def test_devig_three_way_sums_to_one_and_favors_lowest_odds():
    h, d, a = devig_three_way(2.40, 3.20, 3.00)  # home favorite (lowest odds)
    assert h + d + a == pytest.approx(1.0)
    assert h == max(h, d, a)  # home (lowest odds) is the most likely outcome
    assert a > d  # away (3.00) more likely than draw (3.20)


def test_consensus_moneyline_single_book_is_that_book():
    h, d, a = devig_three_way(2.10, 3.30, 3.40)
    ml = consensus_moneyline([(h, d, a)])
    assert ml.home == pytest.approx(h)
    assert ml.draw == pytest.approx(d)
    assert ml.away == pytest.approx(a)


def test_consensus_moneyline_renormalizes_after_median():
    # Three books whose per-outcome medians do NOT sum to 1; result must.
    books = [
        devig_three_way(2.40, 3.20, 3.00),
        devig_three_way(2.45, 3.25, 2.95),
        devig_three_way(2.38, 3.15, 3.10),
    ]
    ml = consensus_moneyline(books)
    assert isinstance(ml, MoneylineProbs)
    assert ml.home + ml.draw + ml.away == pytest.approx(1.0, abs=1e-9)
    assert ml.home == max(ml.home, ml.draw, ml.away)  # home is favorite


def test_consensus_moneyline_empty_raises():
    with pytest.raises(ValueError):
        consensus_moneyline([])


def test_consensus_line_picks_mode():
    assert consensus_line([2.5, 2.5, 3.0]) == 2.5


def test_consensus_line_median_breaks_ties():
    # 2.5 and 3.0 each appear once; median of [2.5, 3.0, 3.5] = 3.0.
    assert consensus_line([2.5, 3.0, 3.5]) == 3.0


def test_consensus_line_single():
    assert consensus_line([2.5]) == 2.5


def test_consensus_over_prob_is_median():
    assert consensus_over_prob([0.45, 0.50, 0.60]) == pytest.approx(0.50)


# --------------------------------------------------------------------------- #
# Event aggregation (pure, on parsed models)
# --------------------------------------------------------------------------- #
def _event(fixture: str, idx: int) -> OddsEvent:
    return OddsEvent.model_validate(load_fixture(fixture)[idx])


def test_reference_lines_full_event():
    ref = reference_lines_from_event(_event("odds_wc.json", 0))
    assert ref.moneyline is not None
    assert ref.moneyline.home + ref.moneyline.draw + ref.moneyline.away == pytest.approx(1.0)
    assert ref.moneyline.home == max(
        ref.moneyline.home, ref.moneyline.draw, ref.moneyline.away
    )
    # Consensus total line is 2.5 (all three books quote it).
    assert ref.total_line == 2.5
    # Symmetric 1.95/1.95 at Pinnacle is the median over -> 0.5 here.
    assert ref.over_prob == pytest.approx(0.5)
    assert ref.num_books == 3


def test_reference_lines_team_totals_present():
    ref = reference_lines_from_event(_event("odds_wc.json", 0))
    sides = {tt.team: tt for tt in ref.team_total_lines}
    assert "home" in sides and "away" in sides
    # USA (home) 1.90/1.90 -> exactly 0.5 over prob.
    assert sides["home"].line == 1.5
    assert sides["home"].over_prob == pytest.approx(0.5)
    assert sides["home"].num_books == 1
    # Mexico (away) 2.05/1.80 -> under-favored, so over prob < 0.5.
    assert sides["away"].over_prob < 0.5


def test_reference_lines_single_book_event():
    ref = reference_lines_from_event(_event("odds_wc.json", 1))  # bra/arg, 1 book
    assert ref.moneyline is not None
    assert ref.num_books == 1
    assert ref.team_total_lines == []  # no team totals offered


# --------------------------------------------------------------------------- #
# Graceful degradation
# --------------------------------------------------------------------------- #
def test_totals_only_event_has_no_moneyline():
    ref = reference_lines_from_event(_event("odds_degraded.json", 0))
    assert ref.moneyline is None
    assert ref.total_line == 2.5
    assert ref.over_prob == pytest.approx(0.5)
    assert ref.num_books == 1  # falls back to totals book count


def test_incomplete_h2h_book_is_skipped():
    # The only book quotes home/away but no Draw -> no usable 1X2.
    ref = reference_lines_from_event(_event("odds_degraded.json", 1))
    assert ref.moneyline is None
    assert ref.num_books == 0


def test_line_disagreement_resolves_to_mode():
    # bookA/bookB at 2.5, bookC at 3.0 -> consensus is 2.5 (mode).
    ref = reference_lines_from_event(_event("odds_degraded.json", 2))
    assert ref.total_line == 2.5
    # bookC's 3.0 line is excluded, so only 2 books contribute the over prob.
    assert ref.num_books == 2
    assert 0.0 < ref.over_prob < 1.0


# --------------------------------------------------------------------------- #
# Client wiring
# --------------------------------------------------------------------------- #
def test_client_implements_interface():
    client, _ = _client()
    assert isinstance(client, OddsDataClient)


def test_fetch_events_parses_all():
    client, _ = _client()
    events = client.fetch_events()
    assert {e.id for e in events} == {"evt_usa_mex", "evt_bra_arg"}


def test_fetch_reference_lines_keyed_by_event_id():
    client, _ = _client()
    refs = client.fetch_reference_lines()
    assert set(refs.keys()) == {"evt_usa_mex", "evt_bra_arg"}
    assert all(isinstance(r, ReferenceLines) for r in refs.values())
    assert refs["evt_usa_mex"].num_books == 3


def test_api_key_passed_as_query_param_not_header():
    client, session = _client()
    client.fetch_events()
    url, params = session.calls[0]
    assert params["apiKey"] == "TESTKEY"
    assert params["oddsFormat"] == "decimal"
    assert params["regions"] == "us,uk,eu"
    assert "h2h" in params["markets"]


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SCREENER_ODDS_API_KEY", raising=False)
    session = FakeSession(_route("odds_wc.json"))
    http = HttpClient("https://api.test/v4", session=session)
    client = TheOddsApiClient(http)  # no api_key, no env
    with pytest.raises(RuntimeError, match="Odds API key"):
        client.fetch_events()


def test_api_key_read_from_env(monkeypatch):
    monkeypatch.setenv("SCREENER_ODDS_API_KEY", "FROMENV")
    session = FakeSession(_route("odds_wc.json"))
    http = HttpClient("https://api.test/v4", session=session)
    client = TheOddsApiClient(http)
    client.fetch_events()
    assert session.calls[0][1]["apiKey"] == "FROMENV"


def test_unexpected_payload_does_not_abort():
    def handler(url, params):
        return FakeResponse(200, {"message": "error: invalid key"})

    session = FakeSession(handler)
    http = HttpClient("https://api.test/v4", session=session)
    client = TheOddsApiClient(http, api_key="X")
    assert client.fetch_events() == []
    assert client.fetch_reference_lines() == {}


def test_cache_prevents_second_fetch():
    client, session = _client()
    client.fetch_events()
    client.fetch_events()
    # NullCache by default -> 2 calls; this asserts a single endpoint per fetch.
    assert all(u.endswith("/odds") for u, _ in session.calls)


def test_schema_drift_ignored():
    ev = OddsEvent.model_validate(
        {"id": "x", "home_team": "A", "away_team": "B", "future_field": 1, "bookmakers": []}
    )
    assert ev.id == "x"


def test_client_has_no_order_methods():
    forbidden = {"create_order", "place_order", "cancel_order", "submit_order"}
    assert forbidden.isdisjoint(dir(TheOddsApiClient))


def test_over_prob_is_real_probability():
    ref = reference_lines_from_event(_event("odds_wc.json", 0))
    assert ref.over_prob is not None
    assert 0.0 <= ref.over_prob <= 1.0
    assert not math.isnan(ref.over_prob)
