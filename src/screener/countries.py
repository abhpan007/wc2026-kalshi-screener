"""Country-name canonicalization for matching teams across data sources.

Kalshi and The Odds API name some nations differently — observed live:
    Kalshi          The Odds API
    Congo DR        DR Congo
    IR Iran         Iran
    Korea Republic  South Korea
    Turkiye         Turkey
    Czechia         Czech Republic

Matching the two feeds on raw names silently drops those matches (no reference
line -> unpriceable). We resolve every name through this alias map to a canonical
token before building the match key, so the variants collide correctly.

Unknown countries fall through to their normalized form, so two identical unknown
names still match; only genuinely-different spellings need an entry here. Add new
aliases as live data surfaces them.
"""

from __future__ import annotations

import re
import unicodedata


def _norm(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Matches the normalization used elsewhere in the pipeline so an unknown name
    canonicalizes identically on both the Kalshi and Odds sides.
    """
    nfkd = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", no_accents.lower())).strip()


# canonical name -> known variants (both feeds' spellings). Canonical is arbitrary
# but stable; what matters is that all variants of one nation share it.
COUNTRY_ALIASES: dict[str, list[str]] = {
    "united states": ["usa", "us", "united states of america", "usmnt"],
    "south korea": ["korea republic", "republic of korea", "korea", "kor"],
    "north korea": ["korea dpr", "dpr korea", "democratic peoples republic of korea"],
    "iran": ["ir iran", "islamic republic of iran"],
    "dr congo": ["congo dr", "democratic republic of the congo", "congo kinshasa"],
    "turkey": ["turkiye"],  # "Türkiye" -> accents stripped -> "turkiye"
    "czech republic": ["czechia"],
    "ivory coast": ["cote divoire", "cote d ivoire", "republic of cote divoire"],
    "bosnia and herzegovina": ["bosnia", "bosnia herzegovina"],
    "cape verde": ["cabo verde"],
    "saudi arabia": ["ksa"],
    "china": ["china pr"],
    "ireland": ["republic of ireland"],
    "united arab emirates": ["uae"],
    "north macedonia": ["macedonia", "fyr macedonia"],
}

# Precompute normalized alias -> normalized canonical (incl. canonical -> itself).
_LOOKUP: dict[str, str] = {}
for _canon, _aliases in COUNTRY_ALIASES.items():
    _LOOKUP[_norm(_canon)] = _norm(_canon)
    for _a in _aliases:
        _LOOKUP[_norm(_a)] = _norm(_canon)


def canonical_country(name: str) -> str:
    """Resolve a country/team name to its canonical normalized token.

    Known aliases map to the shared canonical; unknown names return their own
    normalized form (so identical unknowns still match each other).
    """
    n = _norm(name)
    return _LOOKUP.get(n, n)
