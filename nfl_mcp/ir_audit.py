"""Which players belong in the IR slot, and which must leave it.

Nothing read `reserve_slots` or the `reserve_allow_*` settings, so no tool could
say "move X to IR" or "your IR player is healthy". Both cost something real: an
IR-eligible player parked on the bench blocks a spot a waiver claim could use,
and a player in the IR slot who is no longer eligible makes Sleeper refuse the
roster's adds and claims until he is moved.

Eligibility is Sleeper's own `injury_status`, not the ESPN report. Sleeper is
the one enforcing the rule, so the ESPN reading is shown for context only — a
player ESPN calls Out whom Sleeper still lists Questionable cannot be moved.
"""
from __future__ import annotations

from .errors import create_success_response
from .injury_match import build_injury_index, find_report, misses_this_week, sleeper_injury_status

# Sleeper status -> the league setting that permits it in an IR slot. IR itself
# is always allowed; that is what the slot is.
_ALLOW_SETTING = {
    "Out": "reserve_allow_out",
    "Doubtful": "reserve_allow_doubtful",
    "Sus": "reserve_allow_sus",
    "NA": "reserve_allow_na",
    "DNR": "reserve_allow_dnr",
    "COV": "reserve_allow_cov",
}


def eligible_statuses(settings: dict) -> list[str]:
    """Sleeper statuses this league lets into an IR slot."""
    return ["IR"] + [s for s, key in _ALLOW_SETTING.items() if (settings or {}).get(key)]


def is_ir_eligible(status: str | None, settings: dict) -> bool:
    return bool(status) and status in eligible_statuses(settings)


def audit_roster(
    roster: dict, settings: dict, athletes: dict[str, dict], injury_index: dict
) -> dict:
    """IR moves for one roster: what to activate, what to stash, what is stuck.

    Returns ``{reserve_slots, reserve_used, reserve_free, eligible_statuses,
    moves}``, where each move is ``{action, player, position, sleeper_status,
    report_status, reason}`` and ``action`` is one of:

    - ``activate``: in the IR slot but no longer eligible — Sleeper blocks the
      roster's adds and claims until he leaves it.
    - ``move_to_ir``: eligible, on the active roster, and a slot is free.
    - ``ir_full``: eligible, but every IR slot is taken.
    - ``not_eligible``: will not play this week, yet this league's rules keep
      him out of IR — he holds a bench spot.
    """
    settings = settings or {}
    slots = int(settings.get("reserve_slots") or 0)
    reserve = [str(p) for p in (roster.get("reserve") or [])]
    taxi = {str(p) for p in (roster.get("taxi") or [])}
    free = max(0, slots - len(reserve))
    allowed = eligible_statuses(settings)

    def _entry(pid: str, action: str, reason: str) -> dict:
        row = athletes.get(pid) or {}
        report = find_report(row, injury_index, row.get("team_id"))
        report_status = (report or {}).get("injury_status")
        return {
            "action": action,
            "player": row.get("full_name") or pid,
            "player_id": pid,
            "position": row.get("position"),
            "sleeper_status": sleeper_injury_status(row),
            "report_status": None if report_status == "Active" else report_status,
            "reason": reason,
        }

    moves: list[dict] = []
    for pid in reserve:
        status = sleeper_injury_status(athletes.get(pid))
        if not is_ir_eligible(status, settings):
            moves.append(_entry(
                pid, "activate",
                f"Sleeper lists him {status or 'healthy'}, which this league does not "
                "allow in IR — Sleeper will not process your adds or claims while he "
                "sits there.",
            ))
            free += 1  # activating him frees the slot for someone below

    active = [
        str(p) for p in (roster.get("players") or [])
        if str(p) not in set(reserve) and str(p) not in taxi
    ]
    for pid in active:
        row = athletes.get(pid) or {}
        status = sleeper_injury_status(row)
        if is_ir_eligible(status, settings):
            if free > 0:
                free -= 1
                moves.append(_entry(
                    pid, "move_to_ir",
                    f"{status} is IR-eligible here and a slot is free — moving him "
                    "opens a bench spot for a claim.",
                ))
            else:
                moves.append(_entry(
                    pid, "ir_full",
                    f"{status} is IR-eligible, but all {slots} IR slot(s) are taken."
                    if slots else
                    f"{status} would be IR-eligible, but this league has no IR slot.",
                ))
            continue
        report = find_report(row, injury_index, row.get("team_id"))
        worst = status or (report or {}).get("injury_status")
        if misses_this_week(worst) or misses_this_week((report or {}).get("injury_status")):
            moves.append(_entry(
                pid, "not_eligible",
                f"Will not play this week, but this league only allows "
                f"{', '.join(allowed)} in IR — he holds a bench spot.",
            ))

    return {
        "reserve_slots": slots,
        "reserve_used": len(reserve),
        "reserve_free": max(0, slots - len(reserve)),
        "eligible_statuses": allowed,
        "moves": moves,
    }


async def audit_ir_slots(
    league_id: str, roster_id: int | None = None, user_id: str | None = None
) -> dict:
    """IR audit for one roster in one league."""
    from . import sleeper_tools
    from .database import NFLDatabase

    league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
    if not league:
        return create_success_response({
            "success": False, "error": f"Could not load league {league_id}.",
        })
    rosters = ((await sleeper_tools.get_rosters(league_id)) or {}).get("rosters") or []
    if roster_id is not None:
        mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    elif user_id:
        mine = next((r for r in rosters if r.get("owner_id") == user_id), None)
    else:
        return create_success_response({
            "success": False, "error": "Pass roster_id or user_id to identify which team to audit.",
        })
    if not mine:
        return create_success_response({
            "success": False,
            "error": f"No roster found in league {league_id} for the given identifier.",
        })

    db = NFLDatabase()
    athletes = db.get_athletes_by_ids([str(p) for p in (mine.get("players") or [])])
    audit = audit_roster(
        mine, league.get("settings") or {}, athletes,
        build_injury_index(db.get_all_current_injuries()),
    )
    actions = [m for m in audit["moves"] if m["action"] in ("activate", "move_to_ir")]
    stuck = [m["player"] for m in audit["moves"] if m["action"] == "ir_full"]
    return create_success_response({
        "league": {"league_id": league_id, "name": league.get("name")},
        "roster_id": mine.get("roster_id"),
        **audit,
        "message": (
            f"{len(actions)} IR move(s) to make: "
            + "; ".join(f"{m['action'].replace('_', ' ')} {m['player']}" for m in actions)
            if actions else
            (f"No IR move possible: {', '.join(stuck)} would qualify, but the IR "
             "slot(s) are full — keep, drop, or swap with the player in IR."
             if stuck else "No IR move to make.")
        ),
    })
