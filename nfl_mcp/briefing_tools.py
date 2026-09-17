"""One-call weekly briefing for a single fantasy roster.

Answering "how should I line up this week" otherwise means chaining six calls by
hand — rosters, matchups, league settings, schedule, weather, projections — and
then joining them, which is where week boundaries and team-code variants slip
in. This module does that join once, server-side, against the canonical
sources.

It composes existing tools rather than reimplementing them: projections come
from ``projections``, the optimal lineup from ``win_probability``, the injury
delta from the recorded ``injury_history`` timeline.
"""
from __future__ import annotations

import logging

from .database import NFLDatabase
from .errors import create_success_response
from .teams import normalize_team

logger = logging.getLogger(__name__)

# Sleeper slot names that hold a projectable player. Defenses are priced off
# the opponent's implied total, so they belong in the optimized lineup rather
# than in a separate "not projected" list.
_PROJECTABLE = {"QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "K",
                "WRRB_FLEX", "REC_FLEX", "DEF", "DST"}
_SLOT_RENAME = {"SUPER_FLEX": "SUPERFLEX", "WRRB_FLEX": "FLEX",
                "REC_FLEX": "FLEX", "DEF": "DST"}


def _scoring_label(league: dict) -> str:
    """Sleeper's per-reception value as a scoring label."""
    rec = ((league or {}).get("scoring_settings") or {}).get("rec")
    try:
        rec = float(rec)
    except (TypeError, ValueError):
        return "ppr"
    if rec >= 0.75:
        return "ppr"
    if rec >= 0.25:
        return "half_ppr"
    return "standard"


def _slots_from_positions(roster_positions: list[str] | None) -> dict[str, int]:
    """Count starting slots, ignoring bench/IR/taxi."""
    slots: dict[str, int] = {}
    for raw in roster_positions or []:
        if raw in ("BN", "IR", "TAXI"):
            continue
        slot = _SLOT_RENAME.get(raw, raw)
        slots[slot] = slots.get(slot, 0) + 1
    return slots


def _build_player(
    player_id: str,
    athletes: dict,
    opponents: dict[str, str],
    weather: dict[str, dict],
    usage: dict[str, dict],
) -> dict | None:
    """Projection input for one roster slot, or None if it is not projectable."""
    row = athletes.get(player_id)
    if not row:
        return None
    team = normalize_team(row["team_id"])
    position = row["position"]
    if not team or not position:
        return None
    # Team defenses have no name in the athlete rows; the id *is* the team
    # code, which is what a lineup should display.
    name = row.get("full_name") or (team if position in ("DEF", "DST") else None)
    if not name:
        return None
    opponent = opponents.get(team)
    if not opponent:
        return None  # bye week, or the schedule cache is cold for this team

    player = {
        "name": name,
        "position": position,
        "team": team,
        "opponent": opponent,
        "player_id": player_id,
    }
    if team in weather:
        player["weather"] = weather[team]
    snap = (usage.get(player_id) or {}).get("snap_share")
    if snap is not None:
        player["usage"] = {"snap_percentage": snap}
    return player


async def get_weekly_briefing(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """Everything needed to set a lineup for one week, in a single call."""
    from . import sleeper_tools, weather_tools
    from .injury_service import STATUS_SEVERITY  # noqa: F401  (severity vocabulary)
    from .projections import project_players
    from .win_probability import get_win_probability_lineup

    db = NFLDatabase()

    # 1) Season / week
    if week is None or season is None:
        state = await sleeper_tools.get_nfl_state()
        nfl_state = (state or {}).get("nfl_state") or {}
        week = week or int(nfl_state.get("week") or 1)
        season = season or int(nfl_state.get("season") or 0)

    # 2) League settings drive scoring and slots; guessing them is how a
    #    half-PPR league silently gets full-PPR advice.
    league_resp = await sleeper_tools.get_league(league_id)
    league = (league_resp or {}).get("league") or {}
    scoring = _scoring_label(league)
    slots = _slots_from_positions(league.get("roster_positions"))
    num_teams = int(league.get("total_rosters") or 12)

    # 3) My roster and this week's opponent
    rosters_resp = await sleeper_tools.get_rosters(league_id)
    rosters = (rosters_resp or {}).get("rosters") or []
    if roster_id is None:
        if not user_id:
            return create_success_response({
                "success": False,
                "error": "Pass roster_id or user_id to identify which team to brief.",
            })
        mine = next((r for r in rosters if r.get("owner_id") == user_id), None)
    else:
        mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if not mine:
        return create_success_response({
            "success": False,
            "error": f"No roster found in league {league_id} for the given identifier.",
        })
    roster_id = mine["roster_id"]

    matchups_resp = await sleeper_tools.get_matchups(league_id, week)
    matchups = (matchups_resp or {}).get("matchups") or []
    my_matchup = next((m for m in matchups if m.get("roster_id") == roster_id), None)
    opponent_matchup = None
    if my_matchup:
        opponent_matchup = next(
            (m for m in matchups
             if m.get("matchup_id") == my_matchup.get("matchup_id")
             and m.get("roster_id") != roster_id),
            None,
        )

    # 4) Context shared by every player: opponent, weather, trailing usage.
    #    Opponents come from the cached schedule rather than the odds feed,
    #    which publishes several weeks at once.
    opponents: dict[str, str] = {}
    for team, opp in db.get_week_opponents(season, week).items():
        canon_team, canon_opp = normalize_team(team), normalize_team(opp)
        if canon_team and canon_opp:
            opponents[canon_team] = canon_opp

    weather: dict[str, dict] = {}
    try:
        forecast = await weather_tools.get_weather_forecast(season=season, week=week)
        for game in (forecast or {}).get("games") or []:
            entry = {
                "wind_mph": game.get("wind_mph") or 0.0,
                "precip_in": game.get("precip_in") or 0.0,
                "temp_f": game.get("temp_f"),
                "is_dome": bool(game.get("dome")),
            }
            for side in ("home", "away"):
                team = normalize_team(game.get(side))
                if team:
                    weather[team] = entry
    except Exception as e:  # weather is additive; never fail the briefing on it
        logger.debug(f"weather unavailable for the briefing: {e}")

    usage = {
        row["player_id"]: row
        for row in db.get_usage_for_week(season, max(1, week - 1))
    }
    athletes = db.get_athletes_by_ids(
        list(mine.get("players") or []) + list((opponent_matchup or {}).get("starters") or [])
    )

    # 5) Project mine and the opponent's projected starters.
    #    Reserve (IR) and taxi players cannot legally be started, so they must
    #    not compete for a slot — recommending one produces a lineup the league
    #    will reject.
    unavailable = set(mine.get("reserve") or []) | set(mine.get("taxi") or [])
    my_inputs = [
        p for p in (
            _build_player(pid, athletes, opponents, weather, usage)
            for pid in (mine.get("players") or [])
            if pid not in unavailable
        ) if p
    ]
    opp_ids = (opponent_matchup or {}).get("starters") or []
    opp_inputs = [
        p for p in (
            _build_player(pid, athletes, opponents, weather, usage)
            for pid in opp_ids
        ) if p
    ]

    my_proj = await project_players(
        my_inputs, scoring=scoring, num_teams=num_teams, season=season, week=week
    )
    opp_proj = await project_players(
        opp_inputs, scoring=scoring, num_teams=num_teams, season=season, week=week
    )

    def _as_candidates(result):
        return [
            {
                "name": p["player"], "position": p["position"], "team": p["team"],
                "projected_points": p["projected_points"],
                "floor": p["floor"], "ceiling": p["ceiling"],
            }
            for p in (result or {}).get("projections") or []
        ]

    lineup_slots = {k: v for k, v in slots.items() if k in _PROJECTABLE}
    lineup = await get_win_probability_lineup(
        your_players=_as_candidates(my_proj),
        opponent_players=_as_candidates(opp_proj),
        slots=lineup_slots or None,
    )

    # 6) What to actually change, named rather than left as a diff to eyeball
    # Resolve current starters the same way `_build_player` names them, or a
    # defense — which has no `full_name` — is never recognised as already
    # starting and shows up as a change every single week.
    current_names = set()
    for pid in ((my_matchup or {}).get("starters") or mine.get("starters") or []):
        row = athletes.get(pid)
        if not row:
            continue
        name = row.get("full_name") or normalize_team(row.get("team_id")) or pid
        current_names.add(name)
    recommended = (lineup or {}).get("recommended_lineup") or []
    recommended_names = {s["player"] for s in recommended}
    changes = [
        {"slot": s["slot"], "start": s["player"], "projected_points": s["mean"]}
        for s in recommended if s["player"] not in current_names
    ]
    bench = sorted(current_names - recommended_names)

    # 7) Injury moves on my roster since a week ago
    from datetime import UTC, datetime, timedelta
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    my_player_ids = set(mine.get("players") or [])
    injury_changes = [
        c for c in db.get_injury_status_changes(since=since, limit=500)
        if c.get("player_id") in my_player_ids
    ]

    projected_ids = {p["player_id"] for p in my_inputs}
    unprojectable = [
        # Team defenses have no name in the athlete rows, so fall back to the
        # id, which for a DEF *is* the team code.
        (athletes.get(pid) or {}).get("full_name") or pid
        for pid in (mine.get("players") or [])
        if pid not in projected_ids and pid not in unavailable
    ]
    # Stashed players are listed separately: they are on the roster on purpose,
    # not a gap to fill.
    reserved = [
        (athletes.get(pid) or {}).get("full_name") or pid
        for pid in sorted(unavailable)
    ]

    # Whether live Vegas lines reached the projections. Without them defenses
    # and kickers fall back to a constant and every game-script signal is
    # neutral, which is worth stating rather than leaving to be inferred.
    vegas_active = bool((my_proj or {}).get("vegas_active"))

    return create_success_response({
        "vegas_active": vegas_active,
        "league": {
            "league_id": league_id, "name": league.get("name"),
            "scoring": scoring, "slots": slots, "num_teams": num_teams,
        },
        "week": week,
        "season": season,
        "roster_id": roster_id,
        "record": {
            "wins": (mine.get("settings") or {}).get("wins"),
            "losses": (mine.get("settings") or {}).get("losses"),
        },
        "opponent_roster_id": (opponent_matchup or {}).get("roster_id"),
        "win_probability": (lineup or {}).get("win_probability"),
        "projected_points": (lineup or {}).get("projected_points"),
        "opponent_projected_points": (lineup or {}).get("opponent_projected_points"),
        "recommended_lineup": recommended,
        "changes": changes,
        "bench": bench,
        "injury_changes": injury_changes,
        # Byes, and anything the projection layer could not price.
        "not_projected": unprojectable,
        "reserve": reserved,
    })
