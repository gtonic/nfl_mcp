"""
Playoff odds via Monte-Carlo simulation of the rest of the season.

Turns qualitative standings into real probabilities: simulate every remaining
regular-season matchup thousands of times (each team scores ~ Normal(its
points-per-game, sd)), rank by record then points, and count how often each team
lands in a playoff seed.

Team strength defaults to season points-per-game (from Sleeper roster totals),
which is a simple, robust estimate; when the season hasn't produced enough games
it falls back to the league average.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Tuple

from .sleeper_tools import get_league, get_rosters, get_league_users, get_matchups, get_nfl_state
from .errors import create_success_response, create_error_response, ErrorType, handle_http_errors

logger = logging.getLogger(__name__)

DEFAULT_PLAYOFF_TEAMS = 6
DEFAULT_PLAYOFF_WEEK_START = 15  # regular season = weeks 1..14
DEFAULT_SCORE_SD = 25.0


def _rank_key(w: float, p: float):
    return (w, p)


def _simulate(
    teams: List[Dict], schedule: List[Tuple[int, int]], playoff_teams: int,
    num_sims: int, score_sd: float, rng: random.Random,
) -> Dict[int, Dict[str, float]]:
    """Monte-Carlo the remaining schedule. teams: [{roster_id, wins, points, mean}]."""
    made = {t["roster_id"]: 0 for t in teams}
    seed_sum = {t["roster_id"]: 0 for t in teams}
    ids = [t["roster_id"] for t in teams]
    base_w = {t["roster_id"]: t["wins"] for t in teams}
    base_p = {t["roster_id"]: t["points"] for t in teams}
    mean = {t["roster_id"]: t["mean"] for t in teams}

    for _ in range(num_sims):
        w = dict(base_w)
        p = dict(base_p)
        for a, b in schedule:
            sa = rng.gauss(mean[a], score_sd)
            sb = rng.gauss(mean[b], score_sd)
            p[a] += sa
            p[b] += sb
            if sa >= sb:
                w[a] += 1
            else:
                w[b] += 1
        order = sorted(ids, key=lambda rid: _rank_key(w[rid], p[rid]), reverse=True)
        for seed, rid in enumerate(order[:playoff_teams], 1):
            made[rid] += 1
            seed_sum[rid] += seed

    out = {}
    for rid in ids:
        m = made[rid]
        out[rid] = {
            "playoff_pct": round(m / num_sims * 100, 1),
            "avg_seed": round(seed_sum[rid] / m, 2) if m else None,
        }
    return out


async def _build_remaining_schedule(league_id: str, weeks: List[int]) -> List[Tuple[int, int]]:
    """Reconstruct roster-vs-roster pairings for the given weeks from Sleeper matchups."""
    schedule: List[Tuple[int, int]] = []
    for wk in weeks:
        res = await get_matchups(league_id, wk)
        if not res.get("success"):
            continue
        by_mid: Dict[Any, List[int]] = {}
        for m in res.get("matchups", []):
            mid = m.get("matchup_id")
            rid = m.get("roster_id")
            if mid is None or rid is None:
                continue
            by_mid.setdefault(mid, []).append(rid)
        for rids in by_mid.values():
            if len(rids) == 2:
                schedule.append((rids[0], rids[1]))
    return schedule


@handle_http_errors(default_data={"odds": []}, operation_name="computing playoff odds")
async def get_playoff_odds(
    league_id: str,
    current_week: Optional[int] = None,
    num_sims: int = 10000,
    score_sd: float = DEFAULT_SCORE_SD,
    my_roster_id: Optional[int] = None,
    seed: Optional[int] = None,
    db=None,
) -> Dict:
    """Compute playoff probabilities by simulating the rest of the regular season.

    Args:
        league_id: Sleeper league id.
        current_week: First not-yet-played week (defaults to NFL state / inferred).
        num_sims: Monte-Carlo iterations (default 10000).
        score_sd: Weekly scoring standard deviation (default 25).
        my_roster_id: If given, also returns your win-this-week vs lose-this-week swing.
        seed: RNG seed for reproducibility.

    Returns: {odds: [{roster_id, name, record, mean_ppg, playoff_pct, avg_seed}], ...}
    """
    league_res = await get_league(league_id)
    if not league_res.get("success") or not league_res.get("league"):
        return create_error_response(f"Could not load league: {league_res.get('error')}",
                                     ErrorType.HTTP, {"odds": []})
    league = league_res["league"]
    settings = league.get("settings", {}) or {}
    playoff_teams = int(settings.get("playoff_teams", DEFAULT_PLAYOFF_TEAMS) or DEFAULT_PLAYOFF_TEAMS)
    playoff_week_start = int(settings.get("playoff_week_start", DEFAULT_PLAYOFF_WEEK_START) or DEFAULT_PLAYOFF_WEEK_START)
    regular_weeks = playoff_week_start - 1

    rosters_res = await get_rosters(league_id)
    if not rosters_res.get("success"):
        return create_error_response(f"Could not load rosters: {rosters_res.get('error')}",
                                     ErrorType.HTTP, {"odds": []})
    rosters = rosters_res.get("rosters", [])

    # names
    names = {}
    try:
        users_res = await get_league_users(league_id)
        user_names = {u.get("user_id"): (u.get("display_name") or u.get("metadata", {}).get("team_name"))
                      for u in (users_res.get("users", []) if users_res.get("success") else [])}
        for r in rosters:
            names[r.get("roster_id")] = user_names.get(r.get("owner_id")) or f"Roster {r.get('roster_id')}"
    except Exception:
        pass

    # Build teams with current record + season scoring
    teams = []
    total_ppg = 0.0
    counted = 0
    for r in rosters:
        s = r.get("settings", {}) or {}
        wins = float(s.get("wins", 0) or 0)
        losses = float(s.get("losses", 0) or 0)
        ties = float(s.get("ties", 0) or 0)
        games = wins + losses + ties
        fpts = float(s.get("fpts", 0) or 0) + float(s.get("fpts_decimal", 0) or 0) / 100.0
        mean = (fpts / games) if games > 0 else None
        if mean is not None:
            total_ppg += mean
            counted += 1
        teams.append({
            "roster_id": r.get("roster_id"),
            "wins": wins + 0.5 * ties,   # ties count as half a win for ranking
            "points": fpts,
            "games": games,
            "mean": mean,
            "record": f"{int(wins)}-{int(losses)}" + (f"-{int(ties)}" if ties else ""),
        })
    league_avg_ppg = (total_ppg / counted) if counted else 100.0
    for t in teams:
        if t["mean"] is None:
            t["mean"] = league_avg_ppg

    # current week
    if current_week is None:
        try:
            state = await get_nfl_state()
            current_week = int(state.get("nfl_state", {}).get("week")) if state.get("success") else None
        except Exception:
            current_week = None
    if not current_week or current_week < 1:
        max_games = max((t["games"] for t in teams), default=0)
        current_week = int(max_games) + 1

    remaining_weeks = list(range(current_week, regular_weeks + 1))
    schedule = await _build_remaining_schedule(league_id, remaining_weeks) if remaining_weeks else []

    rng = random.Random(seed)
    sim = _simulate(teams, schedule, playoff_teams, max(100, min(int(num_sims), 50000)), score_sd, rng)

    odds = []
    for t in teams:
        rid = t["roster_id"]
        odds.append({
            "roster_id": rid,
            "name": names.get(rid, f"Roster {rid}"),
            "record": t["record"],
            "mean_ppg": round(t["mean"], 1),
            "playoff_pct": sim[rid]["playoff_pct"],
            "avg_seed": sim[rid]["avg_seed"],
        })
    odds.sort(key=lambda x: x["playoff_pct"], reverse=True)

    result = {
        "odds": odds,
        "playoff_teams": playoff_teams,
        "regular_season_weeks": regular_weeks,
        "current_week": current_week,
        "games_remaining": len(schedule),
        "num_sims": max(100, min(int(num_sims), 50000)),
        "message": (f"Playoff odds over {len(schedule)} remaining games "
                    f"({max(100, min(int(num_sims), 50000))} sims); top {playoff_teams} make it"),
    }

    # Optional: win-this-week vs lose-this-week swing for one team.
    if my_roster_id is not None and schedule:
        my_game = next(((a, b) for (a, b) in schedule if my_roster_id in (a, b)), None)
        if my_game:
            opp = my_game[1] if my_game[0] == my_roster_id else my_game[0]
            rest = [g for g in schedule if g != my_game]

            def _clone(win_rid):
                cloned = []
                for t in teams:
                    c = dict(t)
                    if c["roster_id"] == win_rid:
                        c["wins"] = c["wins"] + 1
                    cloned.append(c)
                return cloned

            win_sim = _simulate(_clone(my_roster_id), rest, playoff_teams, 5000, score_sd, random.Random(seed))
            lose_sim = _simulate(_clone(opp), rest, playoff_teams, 5000, score_sd, random.Random(seed))
            result["this_week_swing"] = {
                "my_roster_id": my_roster_id,
                "opponent_roster_id": opp,
                "if_win_pct": win_sim[my_roster_id]["playoff_pct"],
                "if_lose_pct": lose_sim[my_roster_id]["playoff_pct"],
            }

    return create_success_response(result)
