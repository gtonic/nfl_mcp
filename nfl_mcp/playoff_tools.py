"""
Playoff odds via Monte-Carlo simulation of the rest of the season.

Turns qualitative standings into real probabilities: simulate every remaining
regular-season matchup thousands of times (each team scores ~ Normal(its
points-per-game, sd)), rank by record then points, and count how often each team
lands in a playoff seed.

Team strength defaults to season points-per-game (from Sleeper roster totals),
which is a simple, robust estimate; when the season hasn't produced enough games
it falls back to the league average.

Spread is measured rather than assumed. Every team used to share one hard-coded
weekly sd of 25, which decides how often the weaker team wins and therefore the
whole probability: a consistent roster and a boom/bust one had identical odds at
equal points-per-game. Each team's own weekly scores now set its sd, shrunk
toward the league's pooled spread so a two-game sample cannot claim a team is
metronomic.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from typing import Any

from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .sleeper_tools import get_league, get_league_users, get_matchups, get_nfl_state, get_rosters

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
    """
    deviations: list[float] = []
    for scores in scores_by_team.values():
        if len(scores) < MIN_GAMES_FOR_SD:
            continue
        mean = sum(scores) / len(scores)
        deviations.extend((s - mean) ** 2 for s in scores)
    if len(deviations) < MIN_GAMES_FOR_SD:
        return DEFAULT_SCORE_SD
    return math.sqrt(sum(deviations) / len(deviations))


def _simulate(
    teams: list[dict], schedule: list[tuple[int, int]], playoff_teams: int,
    num_sims: int, score_sd: float | dict[int, float], rng: random.Random,
) -> dict[int, dict[str, float]]:
    """Monte-Carlo the remaining schedule. teams: [{roster_id, wins, points, mean}].

    `score_sd` is either one spread for the whole league or a per-roster mapping.
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
        for a, b in schedule:
            sa = rng.gauss(mean[a], sd.get(a, DEFAULT_SCORE_SD))
            sb = rng.gauss(mean[b], sd.get(b, DEFAULT_SCORE_SD))
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


async def _build_remaining_schedule(league_id: str, weeks: list[int]) -> list[tuple[int, int]]:
    """Reconstruct roster-vs-roster pairings for the given weeks from Sleeper matchups."""
    return [(a, b) for _, a, b in await _build_remaining_schedule_by_week(league_id, weeks)]


async def _build_remaining_schedule_by_week(
    league_id: str, weeks: list[int]
) -> list[tuple[int, int, int]]:
    """``[(week, roster_a, roster_b), ...]`` — the pairings with their week.

    The week is what tells this week's game apart from a later rematch against
    the same opponent; a bare ``(a, b)`` pair cannot.
    """
    schedule: list[tuple[int, int, int]] = []
    for wk in weeks:
        res = await get_matchups(league_id, wk)
        if not res.get("success"):
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
    for wk in weeks:
        try:
            res = await get_matchups(league_id, wk)
        except Exception as e:
            logger.debug(f"weekly scores unavailable for week {wk}: {e}")
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

    rosters_res = await get_rosters(league_id)
    if not rosters_res.get("success"):
        return create_error_response(f"Could not load rosters: {rosters_res.get('error')}",
                                     ErrorType.HTTP, {"odds": []})
    rosters = rosters_res.get("rosters", [])

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
    dated_schedule = (
        await _build_remaining_schedule_by_week(league_id, remaining_weeks)
        if remaining_weeks else []
    )
    schedule = [(a, b) for _, a, b in dated_schedule]

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
                    sd_by_team, rng)

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
        "playoff_teams": playoff_teams,
        "regular_season_weeks": regular_weeks,
        "current_week": current_week,
        "games_remaining": len(schedule),
        "num_sims": max(100, min(int(num_sims), 50000)),
        "score_sd_source": sd_source,
        "league_score_sd": round(prior_sd, 1),
        "message": (f"Playoff odds over {len(schedule)} remaining games "
                    f"({max(100, min(int(num_sims), 50000))} sims); top {playoff_teams} make it"),
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
            rest = schedule[:my_idx] + schedule[my_idx + 1:]

            def _clone(win_rid):
                cloned = []
                for t in teams:
                    c = dict(t)
                    if c["roster_id"] == win_rid:
                        c["wins"] = c["wins"] + 1
                    cloned.append(c)
                return cloned

            win_sim = _simulate(_clone(my_roster_id), rest, playoff_teams, 5000,
                                sd_by_team, random.Random(seed))
            lose_sim = _simulate(_clone(opp), rest, playoff_teams, 5000,
                                 sd_by_team, random.Random(seed))
            result["this_week_swing"] = {
                "my_roster_id": my_roster_id,
                "opponent_roster_id": opp,
                "if_win_pct": win_sim[my_roster_id]["playoff_pct"],
                "if_lose_pct": lose_sim[my_roster_id]["playoff_pct"],
            }

    return create_success_response(result)
