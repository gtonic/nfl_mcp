"""One injury-status vocabulary for every consumer.

The feeds spell the same designation many ways: Sleeper sends short codes
(``Sus``, ``NA``, ``DNR``, ``COV``, ``IR``, ``PUP``, ``NFI``, ``O``, ``D``,
``Q``), ESPN long forms (``Injured Reserve``, ``Reserve/PUP``, ``PUP-R``,
``NFI-R``, ``Suspension``, ``Day-To-Day``), and the practice report its own
(``Did Not Participate``, ``Limited``). Each consumer used to keep a table of
its own and they drifted apart: the injury service ranked ``Injured Reserve``
as an unknown (severity 2), projections priced a raw ``O`` at 0.9, the draft
board gave ``Suspension`` / ``NFI`` / ``Inactive`` full value, and ``Probable``
was healthy in one tool and an open injury in another.

``normalize`` maps any spelling onto one canonical status; ``STATUS_TABLE``
holds everything a consumer needs to know about it. Nothing else should
classify a status string.
"""

from __future__ import annotations

from typing import NamedTuple


class StatusInfo(NamedTuple):
    """What a canonical status means.

    ``availability``: healthy / questionable / doubtful / out / uncertain
    (``Unknown`` -- "we do not know", milder than a real tag). ``severity``:
    the injury service's 1-5 rank. ``multiplier``: the one-week projection
    factor. ``health``: the start/sit health score (0-100).
    """

    availability: str
    severity: int
    multiplier: float
    health: int


STATUS_TABLE: dict[str, StatusInfo] = {
    "Active": StatusInfo("healthy", 1, 1.0, 100),
    "Probable": StatusInfo("healthy", 1, 1.0, 100),
    "FP": StatusInfo("healthy", 1, 1.0, 100),
    "Unknown": StatusInfo("uncertain", 1, 0.95, 70),
    "Questionable": StatusInfo("questionable", 2, 0.9, 60),
    "LP": StatusInfo("questionable", 2, 0.9, 60),
    "DNP": StatusInfo("questionable", 3, 0.9, 60),
    "Doubtful": StatusInfo("doubtful", 4, 0.35, 25),
    "Out": StatusInfo("out", 4, 0.0, 0),
    "Inactive": StatusInfo("out", 4, 0.0, 0),
    "IR": StatusInfo("out", 5, 0.0, 0),
    "PUP": StatusInfo("out", 5, 0.0, 0),
    "NFI": StatusInfo("out", 5, 0.0, 0),
    "Suspended": StatusInfo("out", 5, 0.0, 0),
    "Reserve": StatusInfo("out", 5, 0.0, 0),  # COVID / other reserve lists
    "NA": StatusInfo("out", 5, 0.0, 0),  # Sleeper "not active"
    "DNR": StatusInfo("out", 5, 0.0, 0),  # did not report
}

# A designation none of the tables know. Priced as questionable: Sleeper only
# sets `injury_status` when something is wrong, so an unknown code is evidence
# against the player, not for him.
UNRECOGNISED = StatusInfo("unrecognised", 2, 0.9, 60)

# Exact spellings (lower-cased, stripped) -> canonical.
_ALIASES: dict[str, str] = {
    # healthy
    "active": "Active", "healthy": "Active",
    "probable": "Probable", "p": "Probable",
    "fp": "FP", "full": "FP", "full participation": "FP",
    "full participation in practice": "FP",
    "unknown": "Unknown",
    # questionable
    "questionable": "Questionable", "q": "Questionable", "day-to-day": "Questionable",
    "day to day": "Questionable", "dtd": "Questionable", "gtd": "Questionable",
    "game-time decision": "Questionable", "limited": "Questionable",
    "lp": "LP", "limited participation": "LP", "limited participation in practice": "LP",
    "dnp": "DNP", "did not practice": "DNP", "did not participate": "DNP",
    "did not participate in practice": "DNP",
    # doubtful
    "doubtful": "Doubtful", "d": "Doubtful",
    # out
    "out": "Out", "o": "Out", "inj": "Out", "injured": "Out", "doubtful_out": "Out",
    "inactive": "Inactive",
    "ir": "IR", "i/r": "IR", "injured reserve": "IR", "injured_reserve": "IR",
    "ir-r": "IR", "ir-dfr": "IR", "reserve/injured": "IR",
    "pup": "PUP", "pup-r": "PUP", "pup-p": "PUP", "reserve/pup": "PUP",
    "physically unable to perform": "PUP",
    "nfi": "NFI", "nfi-r": "NFI", "nfi-a": "NFI", "reserve/nfi": "NFI",
    "non-football injury": "NFI", "reserve/non-football injury": "NFI",
    "sus": "Suspended", "susp": "Suspended", "suspended": "Suspended",
    "suspension": "Suspended", "reserve/suspended": "Suspended",
    "reserve-suspended": "Suspended",
    "cov": "Reserve", "covid": "Reserve", "covid-19": "Reserve",
    "reserve/covid-19": "Reserve", "reserve": "Reserve",
    "na": "NA", "not active": "NA",
    "dnr": "DNR", "did not report": "DNR", "reserve/did not report": "DNR",
}

# Prefixes of the long list designations ("Reserve/PUP (knee)", "Inactive
# (injury)", "Out (season)"). Order matters: the specific lists before the
# generic "reserve".
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("injured reserve", "IR"), ("reserve/injured", "IR"), ("ir-", "IR"),
    ("reserve/pup", "PUP"), ("pup", "PUP"), ("physically unable", "PUP"),
    ("reserve/nfi", "NFI"), ("reserve/non-football", "NFI"), ("nfi", "NFI"),
    ("non-football", "NFI"),
    ("reserve/suspend", "Suspended"), ("reserve-suspend", "Suspended"), ("suspen", "Suspended"),
    ("reserve/did not report", "DNR"),
    ("reserve", "Reserve"), ("inactive", "Inactive"), ("out ", "Out"), ("out-", "Out"),
    ("out(", "Out"),
)


def normalize(raw: str | None) -> str | None:
    """Canonical status for any spelling, or None (empty or unrecognised)."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    hit = _ALIASES.get(s)
    if hit:
        return hit
    for prefix, canonical in _PREFIXES:
        if s.startswith(prefix):
            return canonical
    return None


def info(raw: str | None) -> StatusInfo | None:
    """The table row for a status; None for an empty one, UNRECOGNISED for an unknown one."""
    if not (raw or "").strip():
        return None
    canonical = normalize(raw)
    return STATUS_TABLE[canonical] if canonical else UNRECOGNISED


def availability(raw: str | None) -> str:
    """healthy / questionable / doubtful / out / uncertain / unrecognised.

    An empty status is healthy: Sleeper leaves it blank for a healthy player.
    """
    row = info(raw)
    return row.availability if row else "healthy"


def severity(raw: str | None) -> int:
    """The 1-5 severity rank; 0 for an empty status."""
    row = info(raw)
    return row.severity if row else 0


def multiplier(raw: str | None) -> float:
    """One-week availability multiplier; 1.0 for an empty status."""
    row = info(raw)
    return row.multiplier if row else 1.0


def health(raw: str | None) -> int:
    """Start/sit health score; 100 for an empty status."""
    row = info(raw)
    return row.health if row else 100


def is_healthy(raw: str | None) -> bool:
    """A *reported* status that says the player is fine (Active/Probable/FP).

    Empty and ``Unknown`` are not: neither is a clean bill of health.
    """
    row = info(raw)
    return bool(row) and row.availability == "healthy"


def severity_map() -> dict[str, int]:
    """``{spelling: severity}`` over every canonical status and known spelling.

    For SQL that ranks stored strings in a ``CASE`` (exact, case-sensitive
    comparison): canonical names plus each alias as stored by the feeds --
    lower, Title and UPPER case.
    """
    out = {canonical: row.severity for canonical, row in STATUS_TABLE.items()}
    for alias, canonical in _ALIASES.items():
        sev = STATUS_TABLE[canonical].severity
        for spelling in (alias, alias.title(), alias.upper()):
            out.setdefault(spelling, sev)
    # ESPN's mixed-case list names, which neither title() nor upper() produce.
    for spelling in ("Reserve/PUP", "Reserve/NFI", "Reserve/COVID-19", "Day-To-Day"):
        out.setdefault(spelling, STATUS_TABLE[normalize(spelling)].severity)
    return out
