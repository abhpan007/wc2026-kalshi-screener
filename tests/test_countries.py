"""Tests for country-name canonicalization (Kalshi <-> Odds name reconciliation)."""

from __future__ import annotations

import pytest

from screener.countries import canonical_country


@pytest.mark.parametrize(
    "a,b",
    [
        ("Congo DR", "DR Congo"),
        ("IR Iran", "Iran"),
        ("Korea Republic", "South Korea"),
        ("Turkiye", "Turkey"),
        ("Türkiye", "Turkey"),
        ("Czechia", "Czech Republic"),
        ("USA", "United States"),
        ("Cote d'Ivoire", "Ivory Coast"),
        ("Cabo Verde", "Cape Verde"),
    ],
)
def test_variants_resolve_equal(a: str, b: str):
    assert canonical_country(a) == canonical_country(b)


def test_distinct_countries_stay_distinct():
    assert canonical_country("North Korea") != canonical_country("South Korea")
    assert canonical_country("Argentina") != canonical_country("Brazil")


def test_unknown_name_falls_through_normalized():
    # Unknown but identical names still match each other.
    assert canonical_country("Wakanda") == canonical_country("wakanda")
    # Accents/case don't break equality.
    assert canonical_country("Côte") == canonical_country("cote")
