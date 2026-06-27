"""Tests for the shared models and venue resolution (the encoded traps)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from screener.models import CENTRAL, Match, MoneylineProbs, Team
from screener.venues import canonical_venue


def test_moneyline_must_sum_to_one():
    MoneylineProbs(home=0.5, draw=0.3, away=0.2)  # ok
    with pytest.raises(ValidationError):
        MoneylineProbs(home=0.5, draw=0.3, away=0.3)


def _team(i: str) -> Team:
    return Team(team_id=i, name=i.upper())


def test_match_requires_tzaware_kickoff():
    with pytest.raises(ValidationError):
        Match(
            match_id="m1",
            home=_team("usa"),
            away=_team("mex"),
            kickoff_utc=datetime(2026, 6, 21, 20, 0),  # naive
        )


def test_match_kickoff_central_conversion():
    # 02:00 UTC is the prior evening in Central — must not be treated as an error.
    m = Match(
        match_id="m1",
        home=_team("usa"),
        away=_team("mex"),
        kickoff_utc=datetime(2026, 6, 22, 2, 0, tzinfo=ZoneInfo("UTC")),
    )
    central = m.kickoff_central()
    assert central.tzinfo == CENTRAL
    assert central.date().isoformat() == "2026-06-21"


def test_venue_alias_resolution():
    assert canonical_venue("SoFi") == "SoFi Stadium"
    assert canonical_venue("LA Stadium") == "SoFi Stadium"
    assert canonical_venue("NY NJ Stadium") == "MetLife Stadium"
    assert canonical_venue("metlife") == "MetLife Stadium"
    assert canonical_venue("Vancouver Stadium") == "BC Place"
    assert canonical_venue("Boston Stadium") == "Gillette Stadium"
    assert canonical_venue("Unknown Park") is None
