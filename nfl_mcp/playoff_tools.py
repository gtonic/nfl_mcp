"""
Playoff odds via Monte-Carlo simulation of the rest of the season.

Turns qualitative standings into real probabilities: simulate every remaining
regular-season matchup thousands of times (each team scores ~ Normal(its
points-per-game, sd)), rank by record then points, and count how often each team
lands in a playoff seed.

Team strength blends two reads. Points-per-game so far (Sleeper roster totals)
is what the team actually scored, bad lineups and all — after two games it is
mostly noise: a 0-2 team that started the wrong players at 88 a week came out
at 0.0% with twelve games left while its roster projects 115-120. The other
read is forward: each team's best legal lineup, week by week over the rest of
the regular season (``ros.py``, byes and injury absences included, in the
league's scoring). Actual results carry ``games / (games + PROJECTION_PRIOR_GAMES)``
of the weight, so the projection leads early and the record takes over as the
season goes on. Without projections (no athlete cache) it is actuals alone, and
before any games the league average.

Spread is measured rather than assumed. Every team used to share one hard-coded
weekly sd of 25, which decides how often the weaker team wins and therefore the
whole probability: a consistent roster and a boom/bust one had identical odds at
equal points-per-game. Each team's own weekly scores now set its sd, shrunk
toward the league's pooled spread so a two-game sample cannot claim a team is
metronomic.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import statistics
import time
from typing import Any

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .sleeper_tools import (
    get_league,
    get_league_users,
    get_matchups,
    get_nfl_state,
    get_rosters,
    load_rosters,
)

logger = logging.getLogger(__name__)

DEFAULT_PLAYOFF_TEAMS = 6
DEFAULT_PLAYOFF_WEEK_START = 15  # regular season = weeks 1..14
# Fallback weekly scoring spread, used before a league has played enough games
# to measure its own, and as the prior every team's sd is shrunk toward.
DEFAULT_SCORE_SD = 25.0
# Games of league-average evidence mixed into each team's variance. A team needs
# roughly this many of its own before its measured spread outweighs the prior —
# four weeks of fantasy scores are not enough to call a roster steady.
SD_SHRINKAGE_GAMES = 4.0
MIN_GAMES_FOR_SD = 2
# Games-equivalent weight of the roster projection in team strength: the
# shrinkage k = σ²_week / τ², with a weekly team spread σ ≈ 25 and a roster
# projection that misses a team's true level by τ ≈ 8-9 points a game. After
# eight games the scores so far and the projection count the same; by the last
# regular-season week the scores carry ~60%.
PROJECTION_PRIOR_GAMES = 8.0
# Projected strength per (league, season, week): ~250 players' rest of season
# is the expensive part, and it only changes with the week or a roster move.
PROJECTION_CACHE_TTL = 1800.0
_projection_cache: dict[tuple, tuple[float, dict]] = {}


def blend_strength(actual_ppg: float | None, games: float, projected_ppg: float | None,
                   prior_games: float = PROJECTION_PRIOR_GAMES) -> tuple[float | None, float]:
    """``(points per game to simulate, weight on actual results)``.

    Actual points per game shrunk toward the roster projection, the
    projection standing in for ``prior_games`` games of evidence.
    """
    if projected_ppg is None:
        return actual_ppg, 1.0
    if actual_ppg is None or games <= 0:
        return projected_ppg, 0.0
    weight = games / (games + prior_games)
    return weight * actual_ppg + (1 - weight) * projected_ppg, weight


async def _projected_strength(league: dict, league_id: str, rosters: list[dict],
                              season: int | None, week: int, db) -> dict:
    """``{"ppg": {roster_id: points per remaining week}, "weeks": [...]}``.

    Each team's best legal lineup (lineup_slots optimizer) week by week over
    the rest of the regular season, from ROS per-week projections — so a bye
    or an injured starter is covered by that week's next-best player. The
    current week is left out when later ones exist: games already final score
    zero in it. Empty without a database or season. Never raises.
    """
    if db is None or not season or not week:
        return {"ppg": {}, "weeks": []}
    key = (league_id, int(season), int(week),
           tuple(sorted((r.get("roster_id"), tuple(sorted(map(str, r.get("players") or []))))
                        for r in rosters)))
    hit = _projection_cache.get(key)
    if hit and time.monotonic() - hit[0] < PROJECTION_CACHE_TTL:
        return hit[1]
    try:
        from . import ros
        from .roster_needs import lineup_slots

        ids_by_roster: dict[int, list[str]] = {}
        for r in rosters:
            taxi = {str(p) for p in (r.get("taxi") or [])}
            ids_by_roster[r.get("roster_id")] = [
                str(p) for p in (r.get("players") or []) if p and str(p) != "0" and str(p) not in taxi]
        all_ids = [pid for ids in ids_by_roster.values() for pid in ids]
        by_id, meta = await ros.ros_for_ids(all_ids, league=league, season=int(season),
                                            week=int(week), db=db)
        regular = list(meta["windows"]["regular"])
        weeks = [w for w in regular if w > week] or regular
        slots = lineup_slots(league.get("roster_positions"))
        ppg: dict[int, float] = {}
        if weeks and slots:
            for rid, ids in ids_by_roster.items():
                players = [by_id[pid] for pid in ids if pid in by_id]
                if players:
                    ppg[rid] = ros.weekly_lineup_total(players, slots, weeks) / len(weeks)
        out = {"ppg": ppg, "weeks": weeks}
    except Exception as e:
        logger.warning(f"roster projections unavailable for playoff odds: {e}")
        return {"ppg": {}, "weeks": []}
    _projection_cache[key] = (time.monotonic(), out)
    return out


def _rank_key(w: float, p: float):
    return (w, p)


def shrunk_sd(scores: list[float], prior_sd: float,
              prior_games: float = SD_SHRINKAGE_GAMES) -> float:
    """A team's weekly scoring sd, shrunk toward the league's.

    Variances are pooled rather than the standard deviations, because variance is
    what adds. With fewer than two games there is nothing to measure and the
    prior stands on its own.
    """
    if len(scores) < MIN_GAMES_FOR_SD:
        return prior_sd
    sample_var = statistics.variance(scores)
    n = float(len(scores))
    pooled = (n * sample_var + prior_games * prior_sd ** 2) / (n + prior_games)
    return math.sqrt(max(pooled, 0.0))


def league_prior_sd(scores_by_team: dict[int, list[float]]) -> float:
    """The league's own weekly spread, pooled across teams around each team mean.

    Pooling around the *team* mean rather than the league mean keeps this a
    measure of week-to-week volatility, not of how unequal the league is.
    Each team's own mean costs it one degree of freedom: dividing by the game
    count instead halved the variance after two weeks (sd × 0.71), which is
    most of how a 0-2 team came out at 0.0% in week 3.
    """
    squares = 0.0
    dof = 0
    for scores in scores_by_team.values():
        if len(scores) < MIN_GAMES_FOR_SD:
            continue
        mean = sum(scores) / len(scores)
        squares += sum((s - mean) ** 2 for s in scores)
        dof += len(scores) - 1
    if dof < 1:
        return DEFAULT_SCORE_SD
    return math.sqrt(squares / dof)


def _simulate(
    teams: list[dict], schedule: list[tuple[int, int]], playoff_teams: int,
    num_sims: int, score_sd: float | dict[int, float], rng: random.Random,
    median_weeks: list[int] | None = None,
    forced: dict[int, int] | None = None,
) -> dict[int, dict[str, float]]:
    """Monte-Carlo the remaining schedule. teams: [{roster_id, wins, points, mean}].

    `forced` pins the winner of a game (schedule index -> roster id): its
    scores are still drawn — so both teams stay in that week's median game —
    and swapped when needed so the winner has the higher one.

    `score_sd` is either one spread for the whole league or a per-roster mapping.
    `median_weeks` (the week of each game, aligned with `schedule`) turns on the
    median game (Sleeper's ``league_average_match``): each simulated week, the
    top half of that week's scores earns an extra win.
    """
    made = {t["roster_id"]: 0 for t in teams}
    seed_sum = {t["roster_id"]: 0 for t in teams}
    ids = [t["roster_id"] for t in teams]
    base_w = {t["roster_id"]: t["wins"] for t in teams}
    base_p = {t["roster_id"]: t["points"] for t in teams}
    mean = {t["roster_id"]: t["mean"] for t in teams}
    sd = (score_sd if isinstance(score_sd, dict)
          else dict.fromkeys(ids, float(score_sd)))

    for _ in range(num_sims):
        w = dict(base_w)
        p = dict(base_p)
        week_scores: dict[int, list[tuple[float, int]]] = {}
        for i, (a, b) in enumerate(schedule):
            sa = rng.gauss(mean[a], sd.get(a, DEFAULT_SCORE_SD))
            sb = rng.gauss(mean[b], sd.get(b, DEFAULT_SCORE_SD))
            if forced and i in forced and (sa >= sb) != (forced[i] == a):
                sa, sb = sb, sa
            p[a] += sa
            p[b] += sb
            if sa >= sb:
                w[a] += 1
            else:
                w[b] += 1
            if median_weeks is not None:
                week_scores.setdefault(median_weeks[i], []).extend(((sa, a), (sb, b)))
        for scored in week_scores.values():
            scored.sort(reverse=True)
            for _, rid in scored[:len(scored) // 2]:
                w[rid] += 1
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


def _median_kw(median_weeks: list[int] | None) -> dict:
    """``_simulate`` keyword for the median game, only when the league has one."""
    return {} if median_weeks is None else {"median_weeks": median_weeks}


async def _build_remaining_schedule(league_id: str, weeks: list[int]) -> list[tuple[int, int]]:
    """Reconstruct roster-vs-roster pairings for the given weeks from Sleeper matchups."""
    return [(a, b) for _, a, b in await _build_remaining_schedule_by_week(league_id, weeks)]


async def _build_remaining_schedule_by_week(
    league_id: str, weeks: list[int], failed: list[int] | None = None,
) -> list[tuple[int, int, int]]:
    """``[(week, roster_a, roster_b), ...]`` — the pairings with their week.

    The week is what tells this week's game apart from a later rematch against
    the same opponent; a bare ``(a, b)`` pair cannot. Weeks whose matchups
    could not be fetched are appended to ``failed`` (they are not simulated,
    so the caller must say so).
    """
    schedule: list[tuple[int, int, int]] = []
    # Independent reads, one per week: fetched together.
    results = await asyncio.gather(*(get_matchups(league_id, wk) for wk in weeks),
                                   return_exceptions=True)
    for wk, res in zip(weeks, results, strict=True):
        if isinstance(res, BaseException):
            logger.warning(f"matchups unavailable for week {wk}: {res}")
            if failed is not None:
                failed.append(wk)
            continue
        if not res.get("success"):
            if failed is not None:
                failed.append(wk)
            continue
        by_mid: dict[Any, list[int]] = {}
        for m in res.get("matchups", []):
            mid = m.get("matchup_id")
            rid = m.get("roster_id")
            if mid is None or rid is None:
                continue
            by_mid.setdefault(mid, []).append(rid)
        for rids in by_mid.values():
            if len(rids) == 2:
                schedule.append((wk, rids[0], rids[1]))
    return schedule


async def _fetch_weekly_scores(league_id: str, weeks: list[int]) -> dict[int, list[float]]:
    """``{roster_id: [points, ...]}`` for played weeks, from Sleeper matchups.

    A zero is skipped rather than recorded: Sleeper reports 0.0 both for a team
    that scored nothing and for a week it has not published, and treating the
    second as the first would invent volatility no roster actually has.
    """
    scores: dict[int, list[float]] = {}
    results = await asyncio.gather(*(get_matchups(league_id, wk) for wk in weeks),
                                   return_exceptions=True)
    for wk, res in zip(weeks, results, strict=True):
        if isinstance(res, BaseException):
            logger.debug(f"weekly scores unavailable for week {wk}: {res}")
            continue
        if not res.get("success"):
            continue
        for m in res.get("matchups", []):
            rid = m.get("roster_id")
            points = m.get("points")
            if rid is None or points is None:
                continue
            try:
                value = float(points)
            except (TypeError, ValueError):
                logger.warning(
                    f"week {wk}: roster {rid} has unparseable points {points!r}, "
                    "excluded from its scoring spread"
                )
                continue
            if value > 0:
                scores.setdefault(rid, []).append(value)
    return scores


@handle_http_errors(default_data={"odds": []}, operation_name="computing playoff odds")
async def get_playoff_odds(
    league_id: str,
    current_week: int | None = None,
    num_sims: int = 10000,
    score_sd: float | None = None,
    my_roster_id: int | None = None,
    seed: int | None = None,
    db=None,
) -> dict:
    """Compute playoff probabilities by simulating the rest of the regular season.

    Each team's weekly scoring spread is measured from its own played weeks and
    shrunk toward the league's pooled spread, so a boom/bust roster and a steady
    one at the same points-per-game no longer get identical odds.

    Args:
        league_id: Sleeper league id.
        current_week: First not-yet-played week (defaults to NFL state / inferred).
        num_sims: Monte-Carlo iterations (default 10000).
        score_sd: Override the measured spread with one value for every team.
            Leave unset to measure it (falls back to 25 before ~2 games played).
        my_roster_id: If given, also returns your win-this-week vs lose-this-week swing.
        seed: RNG seed for reproducibility.

    Returns: {odds: [{roster_id, name, record, mean_ppg, score_sd, games_scored,
              playoff_pct, avg_seed}], score_sd_source, league_score_sd, ...}
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

    # Records and points are read off the rosters: a cached snapshot is still
    # usable (flagged stale) rather than a hard failure on an upstream blip.
    roster_state = await load_rosters(league_id, "lineup", fetch=get_rosters)
    if roster_state["blocking_error"]:
        return create_error_response(roster_state["blocking_error"], ErrorType.HTTP, {"odds": []})
    rosters = roster_state["rosters"]
    # A median league plays two "games" a week (the opponent and the median),
    # so its records count double against weeks.
    median_game = bool(settings.get("league_average_match"))
    games_per_week = 2 if median_game else 1

    # names
    names = {}
    try:
        users_res = await get_league_users(league_id)
        # Sleeper sends `"metadata": null`, so the `{}` default never applies
        # and `.get` on it raises. One such user used to wipe out the whole
        # name map — and the bare `except: pass` below hid that completely, so
        # every team silently degraded to "Roster 1", "Roster 2".
        user_names = {
            u.get("user_id"): (
                u.get("display_name") or (u.get("metadata") or {}).get("team_name")
            )
            for u in (users_res.get("users", []) if users_res.get("success") else [])
        }
        for r in rosters:
            names[r.get("roster_id")] = user_names.get(r.get("owner_id")) or f"Roster {r.get('roster_id')}"
    except Exception as e:
        logger.warning(f"team names unavailable, falling back to roster ids: {e}")

    # Build teams with current record + season scoring
    teams = []
    total_ppg = 0.0
    counted = 0
    for r in rosters:
        s = r.get("settings", {}) or {}
        wins = float(s.get("wins", 0) or 0)
        losses = float(s.get("losses", 0) or 0)
        ties = float(s.get("ties", 0) or 0)
        # Weeks scored, not decisions: a median league's 2-1 after three weeks
        # is 6 decisions, and points over decisions halved every team's ppg.
        games = (wins + losses + ties) / games_per_week
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
            "actual_ppg": mean,
            "record": f"{int(wins)}-{int(losses)}" + (f"-{int(ties)}" if ties else ""),
        })
    league_avg_ppg = (total_ppg / counted) if counted else 100.0
    for t in teams:
        if t["mean"] is None:
            t["mean"] = league_avg_ppg
    strength_source = "actual" if counted else "league_average"

    # current week
    season = None
    try:
        season = int(league.get("season")) if league.get("season") else None
    except (TypeError, ValueError):
        season = None
    if current_week is None:
        try:
            state = await get_nfl_state()
            nfl_state = state.get("nfl_state", {}) if state.get("success") else {}
            current_week = int(nfl_state.get("week")) if nfl_state.get("week") else None
            season = season or (int(nfl_state["season"]) if nfl_state.get("season") else None)
        except Exception:
            current_week = None
    weeks_played = int(max((t["games"] for t in teams), default=0))
    if not current_week or current_week < 1:
        current_week = weeks_played + 1
    # NFL state still reads the finished week until Sleeper rolls over; the
    # records already include it, so simulating it again double-counted a game.
    current_week = max(current_week, weeks_played + 1)

    remaining_weeks = list(range(current_week, regular_weeks + 1))
    failed_weeks: list[int] = []

    async def _no_projection():
        return {"ppg": {}, "weeks": []}

    async def _no_schedule():
        return []

    # Team strength: actual points per game shrunk toward each roster's
    # projected best lineup for the rest of the regular season. The
    # projection and the schedule are independent reads, fetched together.
    dated_schedule, projected = await asyncio.gather(
        _build_remaining_schedule_by_week(league_id, remaining_weeks, failed_weeks)
        if remaining_weeks else _no_schedule(),
        _projected_strength(league, league_id, rosters, season, current_week, db)
        if remaining_weeks else _no_projection(),
    )
    schedule = [(a, b) for _, a, b in dated_schedule]
    median_weeks = [wk for wk, _, _ in dated_schedule] if median_game else None
    failed_weeks.sort()
    if not schedule:
        projected = {"ppg": {}, "weeks": []}
    for t in teams:
        proj_ppg = projected["ppg"].get(t["roster_id"])
        mean, weight = blend_strength(t["actual_ppg"], t["games"], proj_ppg)
        t["projected_ppg"] = proj_ppg
        t["actual_weight"] = weight
        t["mean"] = mean if mean is not None else league_avg_ppg
    if not projected["ppg"]:
        strength_source = "actual" if counted else "league_average"
    elif counted:
        strength_source = "blended"
    else:
        strength_source = "projected"

    # No remaining games (e.g. preseason / schedule not published) -> the sim
    # would otherwise emit a deterministic 100/0 split by roster id. Flag it.
    if not schedule:
        return create_success_response({
            "odds": [],
            "playoff_teams": playoff_teams,
            "regular_season_weeks": regular_weeks,
            "current_week": current_week,
            "games_remaining": 0,
            "computable": False,
            "failed_weeks": failed_weeks,
            "message": ("Playoff odds can't be computed yet — no remaining scheduled "
                        "games (preseason or schedule not available)."),
        })

    # Weekly spread, per team, from the weeks already played. An explicit
    # `score_sd` overrides it — the caller asked for a specific assumption.
    played_weeks = list(range(1, current_week))
    scores_by_team: dict[int, list[float]] = {}
    if score_sd is None and played_weeks:
        scores_by_team = await _fetch_weekly_scores(league_id, played_weeks)

    if score_sd is not None:
        prior_sd = float(score_sd)
        sd_by_team: float | dict[int, float] = prior_sd
        sd_source = "caller"
    else:
        prior_sd = league_prior_sd(scores_by_team)
        sd_by_team = {
            t["roster_id"]: shrunk_sd(scores_by_team.get(t["roster_id"], []), prior_sd)
            for t in teams
        }
        measured = any(
            len(scores_by_team.get(t["roster_id"], [])) >= MIN_GAMES_FOR_SD for t in teams
        )
        sd_source = "measured" if measured else "default"

    rng = random.Random(seed)
    sim = _simulate(teams, schedule, playoff_teams, max(100, min(int(num_sims), 50000)),
                    sd_by_team, rng, **_median_kw(median_weeks))

    def _team_sd(roster_id) -> float:
        return (sd_by_team.get(roster_id, prior_sd)
                if isinstance(sd_by_team, dict) else sd_by_team)

    odds = []
    for t in teams:
        rid = t["roster_id"]
        odds.append({
            "roster_id": rid,
            "name": names.get(rid, f"Roster {rid}"),
            "record": t["record"],
            "mean_ppg": round(t["mean"], 1),
            # What mean_ppg is made of: points per game so far, the roster's
            # projected best lineup per remaining week, and the weight on the
            # former.
            "actual_ppg": round(t["actual_ppg"], 1) if t.get("actual_ppg") is not None else None,
            "projected_ppg": (round(t["projected_ppg"], 1)
                              if t.get("projected_ppg") is not None else None),
            "actual_weight": round(t.get("actual_weight", 1.0), 2),
            # Reported so a surprising probability can be traced to the spread
            # behind it, and so a small sample is visible as a small sample.
            "score_sd": round(_team_sd(rid), 1),
            "games_scored": len(scores_by_team.get(rid, [])),
            "playoff_pct": sim[rid]["playoff_pct"],
            "avg_seed": sim[rid]["avg_seed"],
        })
    odds.sort(key=lambda x: x["playoff_pct"], reverse=True)

    result = {
        "odds": odds,
        "stale": roster_state["stale"],
        "snapshot_age_seconds": roster_state["snapshot_age_seconds"],
        "warnings": [roster_state["warning"]] if roster_state["warning"] else [],
        "playoff_teams": playoff_teams,
        "regular_season_weeks": regular_weeks,
        "current_week": current_week,
        "games_remaining": len(schedule),
        "num_sims": max(100, min(int(num_sims), 50000)),
        "score_sd_source": sd_source,
        "league_score_sd": round(prior_sd, 1),
        # "blended" (actual shrunk toward projection), "actual" (no
        # projections available), "projected" (no games yet) or
        # "league_average".
        "strength_source": strength_source,
        "strength_method": (
            f"mean_ppg = w × actual_ppg + (1 − w) × projected_ppg, w = games / "
            f"(games + {PROJECTION_PRIOR_GAMES:g}); projected_ppg = best legal lineup "
            "per remaining regular-season week (ROS, byes and injuries included)"
        ),
        "projection_weeks": projected["weeks"],
        # Median leagues: every simulated week also awards a win to the top
        # half of scores.
        "median_game": median_game,
        # Weeks whose matchups could not be fetched: their games are missing
        # from the simulation, so the odds understate the games left.
        "failed_weeks": failed_weeks,
        "message": (f"Playoff odds over {len(schedule)} remaining games "
                    f"({max(100, min(int(num_sims), 50000))} sims); top {playoff_teams} make it"
                    + (f". Matchups for week(s) {', '.join(map(str, failed_weeks))} could not "
                       "be loaded and are not simulated — the odds are incomplete."
                       if failed_weeks else "")),
    }

    # Optional: win-this-week vs lose-this-week swing for one team.
    if my_roster_id is not None and schedule:
        # This week's game only. Matching by pairing alone also removed every
        # later rematch against the same opponent from the simulated rest of
        # the season, as if those games had already been decided.
        my_idx = next((i for i, (wk, a, b) in enumerate(dated_schedule)
                       if wk == current_week and my_roster_id in (a, b)), None)
        if my_idx is not None:
            _, a, b = dated_schedule[my_idx]
            opp = b if a == my_roster_id else a
            # The game stays in the schedule with its winner pinned: dropping
            # it also dropped both teams from this week's median game.
            win_sim = _simulate(teams, schedule, playoff_teams, 5000, sd_by_team,
                                random.Random(seed), **_median_kw(median_weeks),
                                forced={my_idx: my_roster_id})
            lose_sim = _simulate(teams, schedule, playoff_teams, 5000, sd_by_team,
                                 random.Random(seed), **_median_kw(median_weeks),
                                 forced={my_idx: opp})
            result["this_week_swing"] = {
                "my_roster_id": my_roster_id,
                "opponent_roster_id": opp,
                "if_win_pct": win_sim[my_roster_id]["playoff_pct"],
                "if_lose_pct": lose_sim[my_roster_id]["playoff_pct"],
            }

    return create_success_response(result)
