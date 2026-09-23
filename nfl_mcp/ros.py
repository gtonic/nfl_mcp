"""Rest-of-season (ROS) projections: what a player is worth from here on.

Every other projection answers "how many points this week". That is the right
horizon for a lineup and the wrong one for a trade, a drop or an IR stash: a
receiver on bye this week projects zero and looks worthless, an RB out for one
game looks like a cut, and two players worth the same this week can be a month
of starts apart.

For each remaining week of the fantasy season, a player's expected points are

    this week : the weekly projection itself (projections.project_players)
    later     : per_game × matchup(opponent that week)
                    per_game = baseline regressed toward the position prior
                    0 on a bye, 0 inside the expected injury absence

summed separately over the rest of the regular season (``ros_points``) and the
fantasy-playoff window (``playoff_points``, from the league's
``playoff_week_start`` through its last playoff week).

- Baseline: the projection engine's own (trailing opportunity in the league's
  full scoring via ``scoring.py``, rank bucket before there is usage), so ROS
  and the weekly numbers can never disagree about scale.
- Regression: two or three games of opportunity are a small sample. The
  opportunity base is blended with the rank-bucket prior for the position
  (rescaled by ``PRIOR_SCALE`` to what players at that rank score per game),
  weighted by games played (``PRIOR_GAMES`` games-equivalent of prior).
- Matchup: the same position-aware multiplier the weekly projection uses, per
  week, on the defense-vs-position rankings from ``matchup_tools``.
- Byes: from the cached schedule (``schedule_games``); weeks that are not
  cached are fetched from ESPN once and written back.
- Injuries: Out / IR / PUP / suspension count as zero for an expected absence.
  The feeds carry no return date for most players, so the window is read from
  the report text when it states one ("season-ending", "2-4 weeks", "3-game
  suspension") and is otherwise conservative: one week for Out, four for any
  reserve list (the NFL minimum IR stint).
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import UTC, date, datetime

from .errors import create_success_response
from .teams import normalize_team
from .week_context import week_schedule

logger = logging.getLogger(__name__)

DEFAULT_PLAYOFF_WEEK_START = 15
LAST_NFL_WEEK = 18
# Games-equivalent weight of the position prior. With two games of opportunity
# the prior carries half; with a full six-game window it carries a quarter.
# From evals/backtest/ros_backtest.py (2023-24, as of weeks 3-8, predicting the
# per-game rate over the rest of the season): 3 with the raw rank buckets left
# lineup-level starters 1.0-1.3 points/game low at week 3; 2 with the scaled
# prior below is within ±0.3 at weeks 3-4 and has the lowest MAE there.
PRIOR_GAMES = 2
# The rank buckets (`projections.base_ppg`) read low as a per-game-played rate
# for receivers: measured over the rest of the season, a bucket-7.5/9.5/14.5 WR
# scored 10.0/12.0/17.1 and a bucket-6.5/8.5 TE 8.8/10.1 (backtest above). The
# weekly engine only falls back to them before a player has usage; as the
# regression target they pulled every starter down, so the prior is rescaled
# here to the level the players at that rank actually play at.
PRIOR_SCALE = {"QB": 1.0, "RB": 1.08, "WR": 1.18, "TE": 1.25}
# The NFL minimum for a player placed on injured reserve (and PUP/NFI).
IR_MIN_WEEKS = 4
SEASON_ENDING_WEEKS = 99

_SKILL = {"QB", "RB", "WR", "TE"}
_DEFENSE = {"DEF", "DST"}

_SEASON_ENDING_RE = re.compile(
    r"season[- ]ending|(?:rest|remainder) of the (?:\d{4} )?season|"
    r"out for the (?:\d{4} )?season|for the season|miss the (?:\d{4} )?season",
    re.I,
)
_WEEKS_RE = re.compile(r"(\d{1,2})\s*(?:-|to|or)?\s*(\d{1,2})?\s*weeks?\b", re.I)
_GAMES_RE = re.compile(r"(\d{1,2})[- ]game(?:s)?\s+suspension|suspended\s+(\d{1,2})\s+games", re.I)


# --------------------------------------------------------------------------
# League calendar
# --------------------------------------------------------------------------

def playoff_window(settings: dict | None) -> tuple[int | None, int]:
    """``(playoff_week_start, last_week)`` of the fantasy season.

    Sleeper states the first playoff week; the last one follows from the bracket
    size and the round type (0: one week per round, 1: two-week final, 2: two
    weeks per round). A league without playoffs (start 0) ends at week 17.
    """
    settings = settings or {}
    raw_start = settings.get("playoff_week_start", DEFAULT_PLAYOFF_WEEK_START)
    try:
        start = int(raw_start) if raw_start is not None else DEFAULT_PLAYOFF_WEEK_START
    except (TypeError, ValueError):
        start = DEFAULT_PLAYOFF_WEEK_START
    if start <= 0:
        return None, LAST_NFL_WEEK - 1
    try:
        teams = int(settings.get("playoff_teams") or 6)
    except (TypeError, ValueError):
        teams = 6
    rounds = max(1, math.ceil(math.log2(teams))) if teams > 1 else 1
    round_type = int(settings.get("playoff_round_type") or 0)
    weeks = rounds * 2 if round_type == 2 else rounds + 1 if round_type == 1 else rounds
    return start, min(LAST_NFL_WEEK, start + weeks - 1)


def season_windows(settings: dict | None, week: int) -> dict:
    """The remaining regular-season and fantasy-playoff weeks from `week` on."""
    start, last = playoff_window(settings)
    regular_end = (start - 1) if start else last
    regular = list(range(week, regular_end + 1))
    playoff = list(range(max(week, start), last + 1)) if start else []
    return {"regular": regular, "playoff": playoff,
            "playoff_week_start": start, "last_week": last}


def trade_deadline_status(settings: dict | None, week: int | None) -> dict:
    """Whether trades are still open, from the league's ``trade_deadline`` week.

    Sleeper stores the deadline as a week number; 0 or missing means none.
    Trades stay open through the deadline week, so it has passed once the
    league is in a later week. Within one week of it the answer is urgent.
    """
    try:
        deadline = int((settings or {}).get("trade_deadline") or 0)
    except (TypeError, ValueError):
        deadline = 0
    if deadline <= 0 or deadline > LAST_NFL_WEEK or not week:
        return {"deadline_week": deadline or None, "passed": False, "urgent": False,
                "weeks_left": None, "message": "No trade deadline in this league."}
    passed = week > deadline
    weeks_left = deadline - week
    urgent = not passed and weeks_left <= 1
    if passed:
        message = f"The trade deadline (week {deadline}) has passed — no trades can be made."
    elif weeks_left == 0:
        message = f"Trade deadline is THIS week (week {deadline}) — propose now."
    elif urgent:
        message = f"Trade deadline is next week (week {deadline}) — propose this week."
    else:
        message = f"Trade deadline: week {deadline} ({weeks_left} week(s) left)."
    return {"deadline_week": deadline, "passed": passed, "urgent": urgent,
            "weeks_left": None if passed else weeks_left, "message": message}


# --------------------------------------------------------------------------
# Injury window
# --------------------------------------------------------------------------

def _weeks_from_return_date(return_date: str | None, today: date) -> int | None:
    if not return_date:
        return None
    try:
        when = datetime.fromisoformat(str(return_date).replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return max(0, math.ceil((when - today).days / 7))


def expected_absence(
    status: str | None, description: str | None = None,
    return_date: str | None = None, today: date | None = None,
) -> tuple[int, str | None]:
    """``(weeks_missed_from_this_week, reason)`` for an injury designation.

    Questionable and doubtful are priced by the weekly projection (0.9 / 0.35)
    and cost no future weeks. A stated return date wins; then the report text
    ("season-ending", "2-4 weeks", "3-game suspension"); otherwise one week for
    Out and ``IR_MIN_WEEKS`` for any reserve list. The longer reading is taken,
    because a trade or a drop made on an optimistic return is the costly error.
    """
    from .projections import availability  # deferred: projections is heavy

    if availability(status) != "out":
        return 0, None
    s = (status or "").strip().lower()
    text = description or ""
    reserve = (s in ("ir", "injured reserve", "injured_reserve", "pup", "nfi", "reserve")
               or s.startswith(("reserve", "pup", "injured reserve", "nfi")))
    base = IR_MIN_WEEKS if reserve else 1
    reason = (f"{status}: at least {IR_MIN_WEEKS} weeks (NFL minimum reserve stint)"
              if reserve else f"{status}: this week")

    stated = _weeks_from_return_date(return_date, today or datetime.now(UTC).date())
    if stated is not None:
        return max(1, stated), f"{status}: return date {return_date}"
    if _SEASON_ENDING_RE.search(text):
        return SEASON_ENDING_WEEKS, f"{status}: season-ending per the report"
    games = _GAMES_RE.search(text)
    if games and (s in ("sus", "suspended") or "suspen" in s):
        n = int(games.group(1) or games.group(2))
        return max(1, n), f"{status}: {n}-game suspension"
    weeks = [int(b or a) for a, b in _WEEKS_RE.findall(text)]
    if weeks:
        n = max(weeks)
        if 0 < n <= LAST_NFL_WEEK and n > base:
            return n, f"{status}: {n} weeks per the report"
    return base, reason


# --------------------------------------------------------------------------
# Network seams (patched in tests)
# --------------------------------------------------------------------------

async def _fetch_week_schedule(season: int, week: int) -> list[dict]:
    from .sleeper_enrichment import _fetch_week_schedule as fetch
    return await fetch(season, week, force=True)


async def _defense_rankings() -> dict:
    from .matchup_tools import get_defense_analyzer
    return await get_defense_analyzer().fetch_defense_rankings()


def _rows_to_schedule(rows: list[dict]) -> dict[str, str] | None:
    out: dict[str, str] = {}
    for g in rows or []:
        team, opp = normalize_team(g.get("team")), normalize_team(g.get("opponent"))
        if team and opp:
            out[team] = opp
    return out or None


async def schedules_for(db, season: int, weeks: list[int]) -> dict[int, dict[str, str] | None]:
    """``{week: {team: opponent}}``; a missing week is fetched and cached.

    None for a week that is neither cached nor fetchable: byes for it are then
    unknown, and every team is counted as playing.
    """
    out: dict[int, dict[str, str] | None] = {w: week_schedule(db, season, w) for w in weeks}
    missing = [w for w, s in out.items() if s is None]
    if not missing:
        return out
    fetched = await asyncio.gather(
        *(_fetch_week_schedule(season, w) for w in missing), return_exceptions=True
    )
    for w, rows in zip(missing, fetched, strict=True):
        if isinstance(rows, BaseException) or not rows:
            continue
        if db is not None and hasattr(db, "upsert_schedule_games"):
            try:
                db.upsert_schedule_games(rows)
            except Exception as e:
                logger.debug(f"schedule cache warm failed (week {w}): {e}")
        out[w] = week_schedule(db, season, w) or _rows_to_schedule(rows)
    return out


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def regressed_rate(opportunity: float, prior: float, games: int,
                   prior_games: float = PRIOR_GAMES) -> float:
    """The opportunity base blended with the rank prior, weighted by games."""
    weight = prior_games / (games + prior_games) if games + prior_games > 0 else 0.0
    return (1 - weight) * float(opportunity) + weight * float(prior)


def _per_game(proj: dict, position: str, model) -> tuple[float, str, float | None]:
    """``(per_game, source, prior_weight)``: the rate later weeks are priced at."""
    from .projections import base_ppg, defense_base, kicker_base

    if position in _DEFENSE:
        return round(defense_base(None) * model.defense_scale(None), 2), "neutral_defense", None
    if position == "K":
        return round(kicker_base(None) * model.kicker_scale(), 2), "neutral_kicker", None
    bd = proj.get("breakdown") or {}
    base, source = bd.get("base_ppg"), bd.get("base_source")
    if base is None or source in (None, "bye"):
        # No breakdown (a caller-supplied projection): the weekly number is the
        # only rate there is. Undo the injury multiplier so a questionable
        # tag this week does not discount every later week.
        inj = float(bd.get("injury_mult") or 1.0) or 1.0
        return round(float(proj.get("projected_points") or 0.0) / inj, 2), "weekly", None
    usage = float(bd.get("usage_mult") or 1.0)
    if source == "opportunity":
        games = int(bd.get("usage_games") or 0)
        prior = base_ppg(position, bd.get("position_rank"), scoring=model) * PRIOR_SCALE.get(
            position, 1.0)
        weight = PRIOR_GAMES / (games + PRIOR_GAMES)
        rate = regressed_rate(float(base), prior, games)
        return round(rate * usage, 2), "opportunity_regressed", round(weight, 2)
    return round(float(base) * usage, 2), source, None


def _matchup(position: str, opponent: str, rankings: dict, analyzer,
             ppr: float = 1.0) -> tuple[float, str]:
    """The weekly engine's matchup pricing: the continuous factor when the
    rankings carry raw averages, else the tier."""
    from .matchup_tools import matchup_ratio
    from .projections import _ranking_entry, matchup_factor, matchup_multiplier
    if position not in _SKILL or not rankings or analyzer is None:
        return 1.0, "unknown"
    try:
        tier = analyzer.get_matchup_difficulty(position, opponent, rankings).get(
            "matchup_tier", "unknown")
    except Exception:
        tier = "unknown"
    ratio = matchup_ratio(_ranking_entry(rankings, position, opponent), ppr)
    if ratio is not None:
        return matchup_factor(position, ratio), tier
    return matchup_multiplier(position, tier), tier


def _played_this_week(db, season: int, week: int) -> set[str]:
    """Teams whose game this week is already final (their points are banked)."""
    from .game_clock import game_progress
    if db is None or not hasattr(db, "get_week_kickoffs"):
        return set()
    try:
        kickoffs = db.get_week_kickoffs(season, week) or {}
    except Exception:
        return set()
    return {normalize_team(t) or t for t, k in kickoffs.items() if game_progress(k) >= 1.0}


async def ros_projections(
    players: list[dict],
    *,
    season: int,
    week: int,
    settings: dict | None = None,
    scoring="ppr",
    num_teams: int = 12,
    superflex: bool = False,
    db=None,
    include_weekly: bool = False,
    today: date | None = None,
) -> dict:
    """ROS points for `players` (dicts: name, position, team, player_id,
    injury {status, description, return_date}).

    Returns ``{players: [...], windows, schedule_unknown_weeks}``; each player
    has ros_points (rest of the regular season), playoff_points (the fantasy
    playoff window), total_points, weeks_counted, bye_weeks, injury_weeks,
    per_game, baseline_source, prior_weight and — with include_weekly —
    ``weekly: [{week, opponent, points, reason}]``. ``weekly_points`` (a
    ``{week: points}`` map) is always attached for callers that optimise
    week by week.
    """
    from . import projections
    from .scoring import resolve_scoring

    model = resolve_scoring(scoring)
    windows = season_windows(settings, week)
    weeks = sorted(set(windows["regular"]) | set(windows["playoff"]))
    if not weeks:
        return {"players": [], "windows": windows, "schedule_unknown_weeks": []}
    schedules = await schedules_for(db, season, weeks)
    try:
        rankings = await _defense_rankings()
    except Exception as e:
        logger.debug(f"defense rankings unavailable for ROS: {e}")
        rankings = {}
    analyzer = None
    if rankings:
        from .matchup_tools import get_defense_analyzer
        analyzer = get_defense_analyzer()
    played = _played_this_week(db, season, week)

    def _opponent(team: str, w: int) -> str:
        sched = schedules.get(w)
        if sched is None:
            return ""
        return sched.get(team) or "BYE"

    clean = []
    for p in players:
        team = normalize_team(p.get("team")) or (p.get("team") or "").upper()
        position = (p.get("position") or "").upper()
        if not team or not position or not (p.get("name") or p.get("player_name")):
            continue
        clean.append({**p, "team": team, "position": position,
                      "name": p.get("name") or p.get("player_name")})

    # The weekly projection for this week, and for players on bye this week a
    # second one for the week after, so they have a rate to be priced at.
    def _inputs(w: int, subset: list[dict]) -> list[dict]:
        return [{"name": p["name"], "position": p["position"], "team": p["team"],
                 "player_id": p.get("player_id"), "opponent": _opponent(p["team"], w),
                 "injury": {"status": (p.get("injury") or {}).get("status")}}
                for p in subset]

    async def _project(w: int, subset: list[dict]) -> dict[tuple, dict]:
        if not subset:
            return {}
        res = await projections.project_players(
            _inputs(w, subset), scoring=scoring, num_teams=num_teams,
            superflex=superflex, season=season, week=w,
        )
        return {(r.get("player"), r.get("team")): r for r in (res or {}).get("projections") or []}

    now_proj = await _project(week, clean)
    on_bye_now = [p for p in clean
                  if (now_proj.get((p["name"], p["team"])) or {}).get("on_bye")
                  or _opponent(p["team"], week) == "BYE"]
    next_week = next((w for w in weeks if w > week), None)
    rate_proj = await _project(next_week, on_bye_now) if next_week and on_bye_now else {}

    today = today or datetime.now(UTC).date()
    out = []
    for p in clean:
        key = (p["name"], p["team"])
        proj = now_proj.get(key) or {}
        rate_src = rate_proj.get(key) or proj
        per_game, source, prior_weight = _per_game(rate_src, p["position"], model)
        injury = p.get("injury") or {}
        absent, absence_reason = expected_absence(
            injury.get("status"), injury.get("description"), injury.get("return_date"), today)

        ros = playoff = 0.0
        weekly = []
        byes, injured, counted = [], [], 0
        for w in weeks:
            opponent = _opponent(p["team"], w)
            reason = None
            if w == week and p["team"] in played:
                points, reason = 0.0, "already played"
            elif opponent == "BYE" or (w == week and proj.get("on_bye")):
                points, reason = 0.0, "bye"
                byes.append(w)
            elif w - week < absent:
                points, reason = 0.0, absence_reason
                injured.append(w)
            elif w == week and proj:
                points = float(proj.get("projected_points") or 0.0)
            else:
                mult, tier = _matchup(p["position"], opponent, rankings, analyzer, model.rec)
                points = round(per_game * mult, 2)
                if not opponent:
                    reason = "schedule unknown — counted as playing"
                elif tier not in ("unknown", "neutral"):
                    reason = f"{tier} matchup"
            if points > 0:
                counted += 1
            if w in windows["regular"]:
                ros += points
            if w in windows["playoff"]:
                playoff += points
            weekly.append({"week": w, "opponent": opponent or None,
                           "points": round(points, 2), "reason": reason})

        entry = {
            "player": p["name"],
            "player_id": p.get("player_id"),
            "position": p["position"],
            "team": p["team"],
            "ros_points": round(ros, 1),
            "playoff_points": round(playoff, 1),
            "total_points": round(ros + playoff, 1),
            "weeks_counted": counted,
            "this_week_points": round(float(proj.get("projected_points") or 0.0), 1),
            "per_game": per_game,
            "baseline_source": source,
            "prior_weight": prior_weight,
            "bye_weeks": byes,
            "injury_status": injury.get("status"),
            "injury_weeks": injured,
            "injury_window": absence_reason,
            "weekly_points": {row["week"]: row["points"] for row in weekly},
        }
        if include_weekly:
            entry["weekly"] = weekly
        out.append(entry)

    return {
        "players": out,
        "windows": windows,
        "schedule_unknown_weeks": [w for w in weeks if schedules.get(w) is None],
        "matchups_active": bool(rankings),
        "scoring_used": model.summary(),
    }


# --------------------------------------------------------------------------
# Inputs from the athlete cache
# --------------------------------------------------------------------------

def ros_input(row: dict, injury_index: dict) -> dict | None:
    """A ROS input from an ``athletes`` row, with Sleeper + report injury."""
    from .injury_match import find_report, injury_for_row

    team = normalize_team(row.get("team_id") or row.get("team"))
    position = (row.get("position") or "").upper()
    name = row.get("full_name") or row.get("name")
    if not name and position in _DEFENSE:
        # Sleeper's team defenses carry no name in the athlete cache; they are
        # named by their team, as the weekly projection names them. Without
        # this every DEF was dropped from ROS and a DST slot counted as empty.
        name = team
    if not team or not position or not name:
        return None
    injury = injury_for_row(row, injury_index) or {}
    report = find_report(row, injury_index, team) or {}
    return {
        "name": name, "position": "DEF" if position == "DST" else position,
        "team": team, "player_id": str(row.get("id") or row.get("player_id") or ""),
        "injury": {
            "status": injury.get("status"),
            "description": " ".join(
                str(x) for x in (report.get("injury_description"), report.get("game_status"))
                if x),
            "return_date": report.get("return_date"),
        },
    }


async def ros_for_ids(
    ids: list[str], *, league: dict, season: int, week: int, db,
    include_weekly: bool = False,
) -> tuple[dict[str, dict], dict]:
    """``({player_id: ros entry}, meta)`` for Sleeper ids in `league`'s scoring."""
    from .injury_match import build_injury_index
    from .scoring import league_scoring
    from .trade_analyzer_tools import league_format_from_settings

    rows = db.get_athletes_by_ids([str(i) for i in ids]) if db is not None else {}
    try:
        injury_index = build_injury_index(db.get_all_current_injuries()) if db else {}
    except Exception:
        injury_index = {}
    inputs = [x for pid in ids if (row := rows.get(str(pid))) and (x := ros_input(row, injury_index))]
    fmt = league_format_from_settings(league)
    result = await ros_projections(
        inputs, season=season, week=week, settings=league.get("settings") or {},
        scoring=league_scoring(league), num_teams=fmt["num_teams"],
        superflex=fmt["superflex"], db=db, include_weekly=include_weekly,
    )
    by_id = {e["player_id"]: e for e in result["players"] if e.get("player_id")}
    meta = {k: v for k, v in result.items() if k != "players"}
    return by_id, meta


def weekly_lineup_total(players: list[dict], slots: dict[str, int], weeks: list[int]) -> float:
    """Sum over `weeks` of the best legal lineup, each week on that week's points.

    The honest ROS value of a roster: a bye or an injury is covered by whoever
    is next best that week, which a season-total lineup cannot see.
    """
    from .roster_needs import starting_lineup_total
    total = 0.0
    for w in weeks:
        week_players = [
            {"position": p.get("position"),
             "projected_points": (p.get("weekly_points") or {}).get(w, 0.0)}
            for p in players
        ]
        total += starting_lineup_total(week_players, slots)
    return round(total, 1)


def lineup_gains(roster: list[dict], candidate: dict, slots: dict[str, int],
                 week: int, weeks: list[int]) -> dict:
    """What adding `candidate` does to the best lineup, this week and from here on.

    ``roster`` and ``candidate`` are ROS entries (``weekly_points``). Returns
    ``{week_gain, ros_gain, ros_weeks}``: this week's lineup gain, and the
    summed week-by-week gain over `weeks` — so a pickup who only covers a bye
    is worth that one week, not a season.
    """
    from .roster_needs import starting_lineup_total

    def _at(players: list[dict], w: int) -> list[dict]:
        return [{"position": p.get("position"),
                 "projected_points": (p.get("weekly_points") or {}).get(w, 0.0)}
                for p in players]

    week_gain = (starting_lineup_total(_at([*roster, candidate], week), slots)
                 - starting_lineup_total(_at(roster, week), slots))
    ros_gain = (weekly_lineup_total([*roster, candidate], slots, weeks)
                - weekly_lineup_total(roster, slots, weeks)) if weeks else 0.0
    return {"week_gain": round(max(0.0, week_gain), 1),
            "ros_gain": round(max(0.0, ros_gain), 1),
            "ros_weeks": len(weeks)}


# --------------------------------------------------------------------------
# MCP tool
# --------------------------------------------------------------------------

async def get_ros_projections(
    league_id: str,
    player_names: list[str] | None = None,
    player_ids: list[str] | None = None,
    roster_id: int | None = None,
    season: int | None = None,
    week: int | None = None,
    include_weekly: bool = False,
    db=None,
) -> dict:
    """Rest-of-season and fantasy-playoff points in the league's scoring."""
    from . import sleeper_tools
    from .database import NFLDatabase
    from .opportunity_tools import norm_name

    started = datetime.now(UTC)
    if not (player_names or player_ids or roster_id is not None):
        return create_success_response({
            "success": False, "players": [],
            "error": "Pass roster_id, player_ids or player_names.",
        })
    db = db if db is not None else NFLDatabase()
    if week is None or season is None:
        from .week_context import current_season_week
        current = await current_season_week(db)
        week = week or current["week"]
        season = season or current["season"]

    league = ((await sleeper_tools.get_league(league_id)) or {}).get("league") or {}
    if not league:
        return create_success_response({
            "success": False, "players": [], "error": f"Could not load league {league_id}.",
        })

    ids: list[str] = [str(i) for i in (player_ids or [])]
    unresolved: list[str] = []
    slot_of: dict[str, str] = {}
    if roster_id is not None:
        rosters = ((await sleeper_tools.get_rosters(league_id)) or {}).get("rosters") or []
        mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
        if not mine:
            return create_success_response({
                "success": False, "players": [],
                "error": f"No roster {roster_id} in league {league_id}.",
            })
        reserve = {str(p) for p in (mine.get("reserve") or [])}
        taxi = {str(p) for p in (mine.get("taxi") or [])}
        for pid in (str(p) for p in (mine.get("players") or [])):
            ids.append(pid)
            slot_of[pid] = "IR" if pid in reserve else "TAXI" if pid in taxi else "active"
    for name in player_names or []:
        hits = db.search_athletes_by_name(name, limit=10) or []
        exact = [h for h in hits if norm_name(h.get("full_name")) == norm_name(name)]
        pick = next((h for h in exact or hits if h.get("team_id")), None)
        if pick:
            ids.append(str(pick["id"]))
        else:
            unresolved.append(name)
    ids = list(dict.fromkeys(ids))

    by_id, meta = await ros_for_ids(
        ids, league=league, season=season, week=week, db=db, include_weekly=include_weekly)
    entries = []
    for pid in ids:
        entry = by_id.get(pid)
        if not entry:
            continue
        if not include_weekly:
            entry = {k: v for k, v in entry.items() if k != "weekly_points"}
        if pid in slot_of:
            entry["roster_slot"] = slot_of[pid]
        entries.append(entry)
    entries.sort(key=lambda e: e["total_points"], reverse=True)
    windows = meta["windows"]
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return create_success_response({
        "league": {"league_id": league_id, "name": league.get("name")},
        "season": season,
        "week": week,
        "regular_season_weeks": windows["regular"],
        "playoff_weeks": windows["playoff"],
        "players": entries,
        "unresolved": unresolved + [pid for pid in (player_ids or []) if str(pid) not in by_id],
        "schedule_unknown_weeks": meta["schedule_unknown_weeks"],
        "matchups_active": meta["matchups_active"],
        "scoring_used": meta["scoring_used"],
        "elapsed_seconds": round(elapsed, 2),
        "method": (
            "this week = the weekly projection; each later week = per-game "
            f"baseline (opportunity regressed toward the rank prior, {PRIOR_GAMES} "
            "games-equivalent) × that week's matchup multiplier; 0 on byes and "
            "inside the expected injury absence"
        ),
        "caveats": [
            "Injury return windows are estimates: the report text when it states "
            f"one, otherwise 1 week for Out and {IR_MIN_WEEKS} for IR/PUP/NFI.",
            "Later weeks carry no Vegas or weather adjustment — those lines are "
            "not published that far ahead.",
        ] + ([] if meta["matchups_active"] else [
            "No defense rankings available — later weeks are matchup-neutral.",
        ]),
        "message": (
            f"ROS for {len(entries)} player(s): weeks {windows['regular'][0] if windows['regular'] else '-'}"
            f"-{windows['regular'][-1] if windows['regular'] else '-'} regular season, "
            f"{len(windows['playoff'])} playoff week(s)."
        ),
    })


__all__ = [
    "expected_absence",
    "get_ros_projections",
    "lineup_gains",
    "playoff_window",
    "ros_for_ids",
    "ros_input",
    "ros_projections",
    "season_windows",
    "trade_deadline_status",
    "weekly_lineup_total",
]
