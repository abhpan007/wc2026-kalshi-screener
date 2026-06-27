"""World Cup 2026 venue alias resolution.

Kalshi and the sharp books label the same stadium differently (sponsor name vs.
host-city "World Cup" name). We match on a canonical name, resolved through this
alias map. This is reference data, kept here so matching logic stays declarative.

Extend freely; matching is case-insensitive and ignores punctuation/whitespace.
"""

from __future__ import annotations

import re

# canonical name -> set of known aliases (lowercased on lookup)
VENUE_ALIASES: dict[str, list[str]] = {
    "SoFi Stadium": ["SoFi", "Los Angeles Stadium", "LA Stadium"],
    "MetLife Stadium": ["MetLife", "New York New Jersey Stadium", "NY NJ Stadium"],
    "BC Place": ["BC Place Stadium", "Vancouver Stadium"],
    "Lincoln Financial Field": ["Philadelphia Stadium", "The Linc"],
    "Gillette Stadium": ["Foxborough", "Boston Stadium"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Precompute normalized alias -> canonical, including the canonical name itself.
_LOOKUP: dict[str, str] = {}
for _canon, _aliases in VENUE_ALIASES.items():
    _LOOKUP[_norm(_canon)] = _canon
    for _a in _aliases:
        _LOOKUP[_norm(_a)] = _canon


def canonical_venue(name: str) -> str | None:
    """Resolve any known alias to its canonical venue name, or None if unknown."""
    return _LOOKUP.get(_norm(name))
