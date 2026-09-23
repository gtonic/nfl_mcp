"""How last week actually went, against what was projected before it.

The briefing answers "what should I start"; nothing answered "was that right".
This grades a finished week for one roster: each starter's real score against
the projection the lineup was set on, the points that sat on the bench (the
best legal lineup in hindsight, from the same exact optimizer every lineup tool
uses), the result, and whether that hindsight lineup would have changed it.

"Projected" has to mean projected *before the games*. The briefing and
``project_players`` log every pre-kickoff projection (``projection_store``);
the first row of the week is what is graded here. A week that was never
logged is re-projected as a best effort and labelled ``"recomputed"``: that
number uses today's market values and no gameday injury news, so it grades
the model loosely, not the decision that was made.
"""
from __future__ import annotations

import asyncio
import logging

from .briefing_tools import _build_player, _scoring_ppr, find_roster
from .database import NFLDatabase
from .errors import create_success_response
from .lineup_slots import normalize_position, optimal_lineup, starting_slot_list
from .projection_store import scoring_key
from .scoring import league_scoring
from .teams import normalize_team
from .week_context import last_completed_week, week_opponents

logger = logging.getLogger(__name__)

# A miss or hit smaller than this is inside a weekly projection's noise (MAE is
# ~5.8 points) and not worth naming.
_NOTABLE_DIFF = 3.0
_TOP_N = 3


def _position(pid: str, athletes: dict) -> str | None:
    row = athletes.get(pid) or {}
    pos = row.get("position")
    if not pos and pid.isalpha() and pid.isupper() and len(pid) <= 3:
        pos = "DEF"  # a team defense's id is its team code
    return normalize_position(pos) or None


def _label(pid: str, athletes: dict) -> str:
    row = athletes.get(pid) or {}
    return row.get("full_name") or normalize_team(row.get("team_id")) or pid


def _points(matchup: dict | None) -> float | None:
    if not matchup:
        return None
    custom = matchup.get("custom_points")
    value = custom if custom is not None else matchup.get("points")
    return None if value is None else round(float(value), 2)


def _outcome(mine: float | None, theirs: float | None) -> str | None:
    if mine is None or theirs is None:
        return None
    return "win" if mine > theirs else "loss" if mine < theirs else "tie"


def _opponent_of(matchups: list[dict], mine: dict | None) -> dict | None:
    if not mine or mine.get("matchup_id") is None:
        return None
    return next(
        (m for m in matchups
         if m.get("matchup_id") == mine.get("matchup_id")
         and m.get("roster_id") != mine.get("roster_id")),
        None,
    )


async def _recompute(
    db, player_ids: list[str], athletes: dict, league: dict, season: int, week: int,
) -> dict[str, dict]:
    """Best-effort projections for a week that was never logged.

    No database handle goes to the projection, deliberately: that keeps these
    after-the-fact numbers out of the log, and keeps today's injury report
    out of the inputs — it describes this week, not the one being graded.
    """
    from .projections import project_players
    opponents = week_opponents(db, season, week)
    usage = {row["player_id"]: row
             for row in db.get_usage_for_week(season, max(1, week - 1))}
    inputs = []
    for pid in player_ids:
        player = _build_player(pid, athletes, opponents, {}, usage)
        if player:
            # `_build_player` reads Sleeper's *current* injury status; a player
            # hurt since would be projected at zero for a week he played.
            player.pop("injury", None)
            player.pop("injury_detail", None)
            inputs.append(player)
    if not inputs:
        return {}
    result = await project_players(
        inputs, scoring=league_scoring(league),
        num_teams=int(league.get("total_rosters") or 12), season=season, week=week,
    )
    out = {}
    for player, proj in zip(inputs, (result or {}).get("projections") or [], strict=False):
        out[player["player_id"]] = {
            "projected_points": proj.get("projected_points"),
            "floor": proj.get("floor"), "ceiling": proj.get("ceiling"),
        }
    return out


def _grade(pid: str, actual: float | None, proj: dict | None, athletes: dict,
           source: str | None) -> dict:
    entry = {
        "player": _label(pid, athletes),
        "position": _position(pid, athletes),
        "actual": None if actual is None else round(float(actual), 2),
        "projected": None, "floor": None, "ceiling": None,
        "diff": None, "within_range": None, "projection_source": None,
    }
    if proj and proj.get("projected_points") is not None:
        projected = float(proj["projected_points"])
        entry.update({
            "projected": round(projected, 1),
            "floor": proj.get("floor"), "ceiling": proj.get("ceiling"),
            "projection_source": source,
        })
        if actual is not None:
            entry["diff"] = round(float(actual) - projected, 1)
            if proj.get("floor") is not None and proj.get("ceiling") is not None:
                entry["within_range"] = bool(proj["floor"] <= actual <= proj["ceiling"])
    return entry


def calibration_from_pairs(pairs: list[dict]) -> dict:
    """Mean error, mean absolute error and floor/ceiling coverage.

    ``pairs``: ``{position, projected, floor, ceiling, actual}``. Error is
    actual minus projected, so a positive mean says the projections run low.
    """
    def _summary(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {"n": 0, "mean_error": None, "mean_abs_error": None,
                    "within_range_share": None}
        errors = [r["actual"] - r["projected"] for r in rows]
        ranged = [r for r in rows if r.get("floor") is not None and r.get("ceiling") is not None]
        within = sum(1 for r in ranged if r["floor"] <= r["actual"] <= r["ceiling"])
        return {
            "n": n,
            "mean_error": round(sum(errors) / n, 2),
            "mean_abs_error": round(sum(abs(e) for e in errors) / n, 2),
            "within_range_share": round(within / len(ranged), 3) if ranged else None,
        }

    by_position: dict[str, list[dict]] = {}
    for r in pairs:
        by_position.setdefault(r.get("position") or "?", []).append(r)
    return {**_summary(pairs),
            "by_position": {pos: _summary(rows) for pos, rows in sorted(by_position.items())}}


async def league_calibration(
    db, league_id: str, season: int, key: str, through_week: int,
    matchups_by_week: dict[int, list[dict]] | None = None,
) -> dict:
    """Calibration of the logged projections over every finished, logged week.

    Every player in the league's matchups who has a logged projection counts —
    both sides of every matchup the briefing looked at, bench included —
    except byes (projected and scored zero), which would flatter the numbers.
    """
    from . import sleeper_tools
    matchups_by_week = dict(matchups_by_week or {})
    weeks = [w for w in db.get_logged_projection_weeks(season, key) if w <= through_week]
    pairs: list[dict] = []
    missing = [wk for wk in weeks if wk not in matchups_by_week]
    for wk, resp in zip(missing, await asyncio.gather(
        *(sleeper_tools.get_matchups(league_id, wk) for wk in missing)
    ), strict=True):
        matchups_by_week[wk] = (resp or {}).get("matchups") or []
    for wk in weeks:
        points = {}
        for m in matchups_by_week[wk]:
            points.update(m.get("players_points") or {})
        logged = db.get_logged_projections(season, wk, key, player_ids=list(points))
        for pid, row in logged.items():
            actual = points.get(pid)
            projected = row.get("projected_points")
            if actual is None or projected is None:
                continue
            if not projected and not actual:
                continue
            pairs.append({
                "position": normalize_position(row.get("position")) or None,
                "projected": float(projected), "floor": row.get("floor"),
                "ceiling": row.get("ceiling"), "actual": float(actual),
            })
    return {"weeks": weeks, **calibration_from_pairs(pairs),
            "scope": "every player in this league's matchups with a pre-kickoff "
                     "projection logged (byes excluded); error = actual - projected"}


async def get_weekly_retro(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    include_calibration: bool = True,
) -> dict:
    """Grade one finished week for one roster; see the module docstring."""
    from . import sleeper_tools

    db = NFLDatabase()
    week_source = "caller"
    if week is None or season is None:
        done = await last_completed_week(db)
        season = season or done["season"]
        week = week or done["week"]
        week_source = done["source"]
    if not week or week < 1:
        return create_success_response({
            "success": False, "season": season, "week": week,
            "error": "No completed week yet this season.",
        })

    # Independent reads, fetched together.
    league_resp, rosters_resp, matchups_resp = await asyncio.gather(
        sleeper_tools.get_league(league_id),
        sleeper_tools.get_rosters(league_id),
        sleeper_tools.get_matchups(league_id, week),
    )
    league = (league_resp or {}).get("league") or {}
    ppr = _scoring_ppr(league)
    # Only projections made under this league's own scoring are graded.
    key = scoring_key(league_scoring(league))
    rosters = (rosters_resp or {}).get("rosters") or []
    mine_roster, error = find_roster(rosters, league_id, roster_id, user_id)
    if error:
        return create_success_response({"success": False, "error": error})
    roster_id = mine_roster["roster_id"]

    matchups = (matchups_resp or {}).get("matchups") or []
    mine = next((m for m in matchups if m.get("roster_id") == roster_id), None)
    if not mine or not mine.get("players_points"):
        return create_success_response({
            "success": False, "season": season, "week": week, "roster_id": roster_id,
            "error": f"Sleeper has no scored matchup for roster {roster_id} in week {week}.",
        })
    opp = _opponent_of(matchups, mine)

    points = {k: float(v) for k, v in (mine.get("players_points") or {}).items()
              if v is not None}
    starters = [s for s in (mine.get("starters") or []) if s and s != "0"]
    slot_names = starting_slot_list(league.get("roster_positions"))
    slot_of = dict(zip(mine.get("starters") or [], slot_names, strict=False))
    # Taxi players can never be started. Reserve players can't either, but the
    # roster's reserve list is today's, not that week's: someone on IR now who
    # scored that week was on the active roster then.
    taxi = set(mine_roster.get("taxi") or [])
    reserve = set(mine_roster.get("reserve") or [])
    # Starters first, so a bench player who merely tied a starter is not
    # reported as a lineup mistake.
    everyone = starters + [p for p in (mine.get("players") or list(points)) if p not in starters]
    pool_ids = [pid for pid in everyone
                if pid not in taxi and not (pid in reserve and not points.get(pid))]
    opp_starters = [s for s in ((opp or {}).get("starters") or []) if s and s != "0"]
    athletes = db.get_athletes_by_ids(list(set(pool_ids) | set(starters) | set(opp_starters)))

    # Projections: the first pre-kickoff row logged for the week, recomputed
    # only for players never logged.
    wanted = list(set(pool_ids) | set(starters) | set(opp_starters))
    logged = db.get_logged_projections(season, week, key, player_ids=wanted, which="first")
    missing = [pid for pid in set(pool_ids) | set(starters) if pid not in logged]
    recomputed = await _recompute(db, missing, athletes, league, season, week) if missing else {}

    def _proj(pid):
        if pid in logged:
            return logged[pid], "stored"
        if pid in recomputed:
            return recomputed[pid], "recomputed"
        return None, None

    starter_rows = []
    for pid in starters:
        proj, src = _proj(pid)
        row = _grade(pid, points.get(pid), proj, athletes, src)
        starter_rows.append({"slot": slot_of.get(pid), **row})
    bench_rows = []
    for pid in pool_ids:
        if pid in starters:
            continue
        proj, src = _proj(pid)
        bench_rows.append(_grade(pid, points.get(pid), proj, athletes, src))
    bench_rows.sort(key=lambda r: r["actual"] or 0.0, reverse=True)

    sources = {r["projection_source"] for r in starter_rows if r["projection_source"]}
    projection_source = (
        "none" if not sources else sources.pop() if len(sources) == 1 else "mixed"
    )

    # Hindsight: the best legal lineup from everyone who could have started.
    pool = [{"player_id": pid, "position": _position(pid, athletes),
             "actual": points.get(pid, 0.0)} for pid in pool_ids]
    best = optimal_lineup(pool, slot_names, value=lambda p: p["actual"],
                          position=lambda p: p["position"])
    optimal_points = round(sum(p["actual"] for p in best if p), 2)
    started_points = round(sum(points.get(pid, 0.0) for pid in starters), 2)
    best_ids = {p["player_id"] for p in best if p}
    should_start = [
        {"player": _label(p["player_id"], athletes), "slot": slot,
         "actual": round(p["actual"], 2)}
        for slot, p in zip(slot_names, best, strict=False)
        if p and p["player_id"] not in starters
    ]
    should_sit = [
        {"player": _label(pid, athletes), "slot": slot_of.get(pid),
         "actual": round(points.get(pid, 0.0), 2)}
        for pid in starters if pid not in best_ids
    ]

    my_points = _points(mine)
    opp_points = _points(opp)
    outcome = _outcome(my_points, opp_points)
    # Sleeper's total can carry adjustments the per-player points do not;
    # apply the bench points to it rather than to the raw sum.
    optimal_total = (round(my_points + optimal_points - started_points, 2)
                     if my_points is not None else optimal_points)
    optimal_outcome = _outcome(optimal_total, opp_points)

    graded = [r for r in starter_rows if r["diff"] is not None]
    misses = sorted((r for r in graded if r["diff"] <= -_NOTABLE_DIFF), key=lambda r: r["diff"])
    hits = sorted((r for r in graded if r["diff"] >= _NOTABLE_DIFF), key=lambda r: -r["diff"])
    projected_total = (round(sum(r["projected"] for r in starter_rows
                                 if r["projected"] is not None), 1) if graded else None)
    # A starter without a projection counted as 0 made the total look like a
    # full-lineup number when it was not; say how many are missing.
    unprojected_starters = sum(1 for r in starter_rows if r["projected"] is None)

    opp_logged = [logged[pid] for pid in opp_starters if pid in logged]
    opponent = None
    if opp:
        opp_pts = {k: float(v) for k, v in (opp.get("players_points") or {}).items()
                   if v is not None}
        top = max(opp_starters, key=lambda pid: opp_pts.get(pid, 0.0), default=None)
        opponent = {
            "roster_id": opp.get("roster_id"),
            "points": opp_points,
            "projected": (round(sum(r["projected_points"] for r in opp_logged), 1)
                          if len(opp_logged) == len(opp_starters) and opp_logged else None),
            "top_scorer": ({"player": _label(top, athletes),
                            "actual": round(opp_pts.get(top, 0.0), 2)} if top else None),
        }

    calibration = None
    if include_calibration:
        try:
            calibration = await league_calibration(
                db, league_id, season, key, week, matchups_by_week={week: matchups})
        except Exception as e:  # additive; never fail the retro on it
            logger.debug(f"calibration unavailable: {e}")

    margin = (round(my_points - opp_points, 2)
              if my_points is not None and opp_points is not None else None)
    return create_success_response({
        "league": {"league_id": league_id, "name": league.get("name"), "ppr": ppr,
                   "scoring_key": key},
        "season": season,
        "week": week,
        "week_source": week_source,
        "roster_id": roster_id,
        "result": {"points": my_points, "opponent_points": opp_points,
                   "outcome": outcome, "margin": margin},
        "projected_total": projected_total,
        "projected_total_partial": bool(projected_total is not None and unprojected_starters),
        "unprojected_starters": unprojected_starters,
        "projection_source": projection_source,
        "starters": starter_rows,
        "bench": bench_rows,
        "hindsight": {
            "optimal_points": optimal_total,
            "points_left_on_bench": round(optimal_points - started_points, 2),
            "should_have_started": should_start,
            "should_have_sat": should_sit,
            "optimal_outcome": optimal_outcome,
            "would_have_flipped": bool(outcome and optimal_outcome
                                       and optimal_outcome != outcome),
        },
        "biggest_misses": misses[:_TOP_N],
        "biggest_hits": hits[:_TOP_N],
        "opponent": opponent,
        "calibration": calibration,
        "notes": (
            ["Some projections were recomputed after the fact (no pre-kickoff "
             "projection was logged): they use current market values and no "
             "gameday injury news, so they grade the model loosely, not the "
             "lineup decision."]
            if "recomputed" in {projection_source, "mixed"} or recomputed else []
        ),
    })
