"""Join injury reports to Sleeper players across the two id spaces.

`player_injuries`, `injury_history` and `player_practice_status` carry ESPN
athlete ids; `athletes` and every roster carry Sleeper ids. The two are
unrelated numbers — Jayden Daniels is 4426348 in one and 11566 in the other —
and 12 of ~2600 report rows collide by accident with a *different* Sleeper
player (ESPN 8439 is Aaron Rodgers, Sleeper 8439 is Demetris Robertson). So a
lookup by id finds nothing for the player asked about and, occasionally, a
stranger's injury. The join has to go through (normalized name, team), using
the same normalization the nflverse lookup uses.
"""

from __future__ import annotations

import json

from .injury_service import worst_status
from .opportunity_tools import norm_name
from .teams import normalize_team


def sleeper_injury_status(row: dict | None) -> str | None:
    """Sleeper's injury status for an athlete row, or None.

    Read from the stored raw payload: the top-level `status` column carries
    roster status ("Active"/"Inactive"), which is a different question.
    """
    if not row:
        return None
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return (raw or {}).get("injury_status") if isinstance(raw, dict) else None


def build_injury_index(injuries: list[dict]) -> dict[tuple[str, str], dict]:
    """Index the multi-source injury reports by (normalized name, team)."""
    index: dict[tuple[str, str], dict] = {}
    for row in injuries:
        name = norm_name(row.get("player_name"))
        team = normalize_team(row.get("team_id")) or ""
        if name:
            index[(name, team)] = row
    return index


def find_report(
    athlete_row: dict | None, injury_index: dict[tuple[str, str], dict], team: str | None
) -> dict | None:
    """The injury report row for a Sleeper athlete, or None."""
    name = norm_name((athlete_row or {}).get("full_name"))
    if not name:
        return None
    return injury_index.get((name, normalize_team(team) or ""))


def report_ids_for(athlete_rows: list[dict], injury_index: dict[tuple[str, str], dict]) -> list[str]:
    """ESPN report ids for Sleeper athletes — the ids `injury_history` is keyed by."""
    ids = []
    for row in athlete_rows:
        report = find_report(row, injury_index, row.get("team_id"))
        if report and report.get("player_id"):
            ids.append(str(report["player_id"]))
    return ids


def resolve_injury(
    athlete_row: dict | None, injury_index: dict[tuple[str, str], dict], team: str
) -> dict | None:
    """Combine Sleeper's status with the ESPN/CBS report, worst case wins.

    Returns ``{status, source, sleeper_status, report_status, injury_type}``
    or None when neither source says anything. Disagreement is the
    normal state around kickoff, not an anomaly: on 2026-09-20 ESPN had Brock
    Bowers at doubtful while Sleeper still said questionable, and taking the
    milder reading put a 0.9 multiplier on a player who did not play.
    """
    sleeper_status = sleeper_injury_status(athlete_row)
    report = find_report(athlete_row, injury_index, team)
    report_status = (report or {}).get("injury_status")
    # "Active" is the report's way of saying "no injury", so it must not beat a
    # real designation from the other source.
    if report_status == "Active":
        report_status = None

    status = worst_status(sleeper_status, report_status)
    if not status:
        return None
    return {
        "status": status,
        "source": ("both" if sleeper_status and report_status
                   else "report" if report_status else "sleeper"),
        "sleeper_status": sleeper_status,
        "report_status": report_status,
        "injury_type": (report or {}).get("injury_type"),
    }
