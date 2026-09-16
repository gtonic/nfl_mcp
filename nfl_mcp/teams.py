"""Canonical NFL team codes and the one place that maps variants onto them.

Every upstream source spells a few teams differently — Sleeper's athlete rows
say ``WAS`` and ``OAK``, the odds feed and ESPN say ``WSH`` and ``LV``, nflverse
says ``LA`` for the Rams — and relocations leave ``SD``/``STL``/``OAK`` in
historical data. Left unnormalized these do not raise; they silently fail to
join, which is how a healthy quarterback once looked like he was on a bye.

This module owns the mapping. Callers at a source boundary run values through
``normalize_team`` instead of keeping a private alias dict, because private
alias dicts drift.
"""
from __future__ import annotations

# The 32 codes this codebase uses internally.
CANONICAL_TEAMS: frozenset[str] = frozenset({
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WSH",
})

# Variant -> canonical. Covers source spellings and relocations.
TEAM_ALIASES: dict[str, str] = {
    "WAS": "WSH", "WASHINGTON": "WSH",
    "JAC": "JAX", "JACKSONVILLE": "JAX",
    "LA": "LAR", "RAMS": "LAR", "STL": "LAR",
    "OAK": "LV", "RAIDERS": "LV",
    "SD": "LAC", "CHARGERS": "LAC",
    "TAM": "TB", "GNB": "GB", "KAN": "KC", "NWE": "NE",
    "NOR": "NO", "SFO": "SF", "ARZ": "ARI", "BLT": "BAL",
    "CLV": "CLE", "HST": "HOU",
}

FULL_NAME_TO_CODE: dict[str, str] = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WSH",
}

CODE_TO_FULL_NAME: dict[str, str] = {v: k for k, v in FULL_NAME_TO_CODE.items()}

_UPPER_FULL_NAMES = {k.upper(): v for k, v in FULL_NAME_TO_CODE.items()}


def normalize_team(value: str | None) -> str | None:
    """Canonical team code for any known spelling, or None.

    Accepts a code (``"was"``, ``"WSH"``), an alias (``"OAK"``) or a full team
    name (``"Las Vegas Raiders"``). Returns None for empty input and for values
    that are not recognisable teams — free agents carry ``""`` in the athlete
    rows, and a caller must be able to tell that apart from a real team.
    """
    if not value:
        return None
    key = value.strip().upper()
    if not key:
        return None
    if key in CANONICAL_TEAMS:
        return key
    if key in TEAM_ALIASES:
        return TEAM_ALIASES[key]
    if key in _UPPER_FULL_NAMES:
        return _UPPER_FULL_NAMES[key]
    # City or partial name ("Kansas City", "Buccaneers"). Only accepted when it
    # identifies exactly one team — "New York" matches two, and guessing is
    # worse than admitting the value is ambiguous.
    matches = {code for name, code in _UPPER_FULL_NAMES.items() if key in name}
    if len(matches) == 1:
        return matches.pop()
    return None
