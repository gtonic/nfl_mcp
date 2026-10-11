"""Rest-of-season (ROS) projections: what a player is worth from here on.

Every other projection answers "how many points this week". That is the right
horizon for a lineup and the wrong one for a trade, a drop or an IR stash: a
receiver on bye this week projects zero and looks worthless, an RB out for one
game looks like a cut, and two players worth the same this week can be a month
of starts apart.

For each remaining week of the fantasy season, a player's expected points are

    this week : the weekly projection itself (projections.project_players)
    later     : ROS_MODEL_WEIGHT × ours + (1 − ROS_MODEL_WEIGHT) × Sleeper(week)
                    ours = per_game × matchup(opponent that week)
                    per_game = baseline regressed toward the position prior
                    Sleeper(week) = Sleeper's projection for that week
                    0 on a bye, 0 inside the expected injury absence

summed separately over the rest of the regular season (``ros_points``) and the
fantasy-playoff window (``playoff_points``, from the league's
``playoff_week_start`` through its last playoff week).

- Baseline: the projection engine's own (trailing opportunity in the league's
  full scoring via ``scoring.py``, rank bucket before there is usage), so ROS
  and the weekly numbers can never disagree about scale.
- Regression: two or three games of opportunity are a small sample. The
  opportunity base is blended with the rank-bucket prior for the position
  (what players at that rank score per game played), weighted by games played (``PRIOR_GAMES`` games-equivalent of prior).
- Matchup: the same position-aware multiplier the weekly projection uses, per
  week, on the defense-vs-position rankings from ``matchup_tools``.
- Byes: from the cached schedule (``schedule_games``); weeks that are not
  cached are fetched from ESPN once and written back.
- Injuries: Out / IR / PUP / suspension count as zero for an expected absence.
  The feeds carry no return date for most players, so the window is read from
  the report text when it states one ("season-ending", "2-4 weeks", "3-game
  suspension") and is otherwise conservative: one week for Out, four for any
  reserve list (the NFL minimum IR stint, counted from the placement recorded
  in ``injury_history`` when there is one). Windows other than a stated return
  date count games, so a bye inside one does not shorten it.
- Inherited volume: a backup's weekly base can include a share of an absent
  starter's volume; later weeks carry it only for that starter's expected
  absence and are otherwise priced on the backup's own volume.
- Returning teammates: the reverse. A backup whose recent games came while a
  higher-valued teammate was out keeps that bigger rate only until the
  teammate's expected return (``returning_teammates`` in the weekly
  breakdown); from then on he is priced on the games they played together
  (``deflated_base_ppg``), regressed toward the rank prior like any base.
- Backup quarterback: a receiver whose starting QB is out keeps the weekly
  projection's model multiplier (``qb_context``, `qb_coupling`) for the
  starter's expected absence. "Week-to-week" in an Out player's report is
  read as two games, not one.
- News: the weekly projection's ``news_flags`` (`news_signals`) are passed
  through on every entry, for the tools that weigh a role's security
  (waivers, drops, value trajectory); they do not move ROS points, except
  that a ``multi_week_absence`` flag's parsed length ("a six-week
  recovery", "eligible to return in Week 8") sets the absence window of an
  Out / reserve player the injury feed gives no return date (precedence:
  ESPN return date > parsed news weeks > status heuristics; see
  `expected_absence`).
- K / DEF: later weeks are priced per opponent off the offense read the weekly
  engine falls back to (``streaming_tools.unit_matchup``).
- Sleeper's later weeks: Sleeper publishes a projection for every week of the
  season (``api.sleeper.app/projections/nfl/<season>/<week>``), refreshed for
  all of them at once, so each later week is blended the same Sleeper-first way
  as this one (``ROS_MODEL_WEIGHT`` ours; ``ros_source`` / ``weekly[].source``
  say which weeks were). Fetched in parallel for up to
  ``ROS_SLEEPER_WEEKS_AHEAD`` weeks and cached 12 h per week
  (``sleeper_projections.fetch_weeks``). What each side carries, so nothing is
  counted twice:

  * ours: the matchup multiplier, the returning-teammate deflation and the
    inherited volume (Sleeper's line has its own opponent and depth chart, so
    neither is applied to its share);
  * Sleeper's share × the role multiplier (as on this week's blend: its line
    lags a role change) and, through the starter's expected absence, the
    backup-quarterback multiplier (`qb_coupling`: its line does not move for
    one);
  * injuries: our absence window is zero whatever Sleeper says; after it, a
    hurt player Sleeper lists without points stays at zero until the first
    week it projects him again (its return timeline); one it never projects
    again in the fetched weeks is priced on the model alone (no return date
    from either source). A healthy player Sleeper lists without points (a
    backup) keeps ``ROS_MODEL_WEIGHT`` of ours. No Sleeper row: the model alone.

  Backtest (``evals/backtest/ros_sleeper_backtest.py``, 2023-25, as of weeks
  4/6/8/10, n=3904, per-game rate over the rest of the season): our rate MAE
  2.79; with Sleeper's week-W line as a flat stand-in for its later weeks
  (leak-free, the lower bound — Sleeper keeps no history of its look-ahead
  numbers) the 0.25 blend is 2.69 (QB 3.75, RB 2.82, WR 2.63, TE 2.15; the
  weight's best, 0.4, is 2.67); with each later week's own pre-game Sleeper
  line (the upper bound, not leak-free) 2.04.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import UTC, date, datetime

from .errors import create_success_response
from .teams import normalize_team
from .week_context import MIN_TEAMS_FOR_KNOWN_WEEK, week_schedule

logger = logging.getLogger(__name__)

DEFAULT_PLAYOFF_WEEK_START = 15
LAST_NFL_WEEK = 18
# Games-equivalent weight of the position prior. With two games of opportunity
# the prior carries half; with a full six-game window it carries a quarter.
# From evals/backtest/ros_backtest.py (2023-24, as of weeks 3-8, predicting the
# per-game rate over the rest of the season): 3 with the raw rank buckets left
# lineup-level starters 1.0-1.3 points/game low at week 3; 2 with the
# recalibrated buckets is within ±0.3 overall at weeks 3-6 and has the lowest
# MAE there.
PRIOR_GAMES = 2
# The prior is the weekly engine's own rank bucket (`projections.base_ppg`),
# calibrated as points per game played, so ROS and the weekly numbers share one
# scale. (Until the buckets were recalibrated a ROS-only `PRIOR_SCALE` of up to
# 1.25 made up for WR/TE buckets that read low.)
# The NFL minimum for a player placed on injured reserve (and PUP/NFI).
IR_MIN_WEEKS = 4
SEASON_ENDING_WEEKS = 99
# Later weeks: our share of the blend with Sleeper's projection for that week
# (the rest is Sleeper's). The weekly blend's weight, kept for ROS by
# evals/backtest/ros_sleeper_backtest.py (see the module doc).
ROS_MODEL_WEIGHT = 0.25
# How many weeks past the current one Sleeper's projections are fetched for;
# weeks beyond it are priced on the model alone. Sleeper publishes every week
# of the regular season, so the default covers the whole fantasy season.
ROS_SLEEPER_WEEKS_AHEAD = 14

_SKILL = {"QB", "RB", "WR", "TE"}
_DEFENSE = {"DEF", "DST"}

_SEASON_ENDING_RE = re.compile(
    r"(?:season[- ]ending|(?:rest|remainder) of the (?:\d{4} )?season|"
    r"out for the (?:\d{4} )?season|for the season|miss the (?:\d{4} )?season)"
    # "questionable for the season opener" is one game, not the year.
    r"(?![- ]?(?:opener|debut|finale))",
    re.I,
)
_WEEKS_RE = re.compile(r"(\d{1,2})\s*(?:-|to|or)?\s*(\d{1,2})?\s*weeks?\b", re.I)
_GAMES_RE = re.compile(r"(\d{1,2})[- ]game(?:s)?\s+suspension|suspended\s+(\d{1,2})\s+games", re.I)
# An Out player the report calls "week-to-week" is not back next week: the
# phrase is the coaches' way of saying "more than one". Two games, the
# shortest reading of it (`news_signals` flags the same phrase).
_WEEK_TO_WEEK_RE = re.compile(r"\bweek[- ]to[- ]week\b", re.I)
WEEK_TO_WEEK_GAMES = 2
# "Could miss multiple games" (Rapoport on Lamar Jackson, week 5 2026) says
# the same as week-to-week, and is priced the same.
_MULTI_GAME_RE = re.compile(
    r"\bmiss(?:es|ing)?\s+(?:multiple|several|a few|a couple(?: of)?|some)\s+"
    r"(?:more\s+)?(?:games|weeks|contests)\b|\bmulti(?:ple)?[- ]week\b|\bmulti[- ]game\b",
    re.I)


def multi_game_phrase(text: str | None) -> str | None:
    """"week-to-week" / "multiple games" when the report text says the
    absence runs past this week, else None. Any status: a Questionable
    quarterback "could miss multiple games" is read the same way
    (`qb_coupling`)."""
    if not text:
        return None
    if _WEEK_TO_WEEK_RE.search(text):
        return "week-to-week"
    if _MULTI_GAME_RE.search(text):
        return "multiple games"
    return None


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


def is_reserve(status: str | None) -> bool:
    """True for an IR / PUP / NFI / other reserve-list designation."""
    s = (status or "").strip().lower()
    return (s in ("ir", "injured reserve", "injured_reserve", "pup", "nfi", "reserve")
            or s.startswith(("reserve", "pup", "injured reserve", "nfi")))


def is_preseason_list(status: str | None) -> bool:
    """True for PUP / NFI: lists a player can only carry over from the
    preseason, so their stint starts before week 1."""
    s = (status or "").strip().lower().replace("reserve/", "").replace("reserve-", "")
    return s.startswith(("pup", "nfi", "physically unable", "non-football"))


def expected_absence(
    status: str | None, description: str | None = None,
    return_date: str | None = None, today: date | None = None,
    placed_on: date | None = None, season_week: int | None = None,
    news_weeks: int | None = None, news_reason: str | None = None,
) -> tuple[int, str | None]:
    """``(weeks_missed_from_this_week, reason)`` for an injury designation.

    Precedence: ESPN's return date > the absence length parsed from the news
    (`news_weeks`: `news_signals.absence_weeks` over the player's
    ``multi_week_absence`` flags -- "a six-week recovery", "eligible to
    return in Week 8", "placed on injured reserve", "season-ending" -- and
    this report's own text) > the status heuristics (one week for Out, the
    reserve minimum, two for "week-to-week"). Between the news and the report
    text the longer reading wins; neither shortens a reserve minimum.

    Questionable and doubtful are priced by the weekly projection (0.9 / 0.35)
    and cost no future weeks. A stated return date (ESPN's ``returnDate``)
    wins, except that it never shortens a reserve list's minimum stint; then
    the report text
    ("season-ending", "2-4 weeks", "3-game suspension"); otherwise one week for
    Out and ``IR_MIN_WEEKS`` for any reserve list. The longer reading is taken,
    because a trade or a drop made on an optimistic return is the costly error.

    `placed_on` is when the reserve designation was first seen (see
    `reserve_since`): the minimum stint counts from then rather than from
    today, so a player three weeks into IR is not stashed for four more.

    `season_week`: a PUP / NFI player has been on the list since before
    week 1 (only a preseason list player can stay on it), so his minimum is
    served once ``season_week - 1`` games are played, whether or not the
    placement was recorded -- then the return date or "this week" decides.
    """
    from .projections import availability  # deferred: projections is heavy

    if availability(status) != "out":
        return 0, None
    s = (status or "").strip().lower()
    text = description or ""
    reserve = is_reserve(status)
    base = IR_MIN_WEEKS if reserve else 1
    reason = (f"{status}: at least {IR_MIN_WEEKS} weeks (NFL minimum reserve stint)"
              if reserve else f"{status}: this week")
    if reserve and placed_on is not None:
        served = max(0, ((today or datetime.now(UTC).date()) - placed_on).days // 7)
        if served:
            base = max(1, IR_MIN_WEEKS - served)
            reason = (f"{status}: {base} more week(s) of the {IR_MIN_WEEKS}-week minimum "
                      f"(on the list since {placed_on.isoformat()})")
    if reserve and season_week and is_preseason_list(status):
        played = max(0, int(season_week) - 1)
        if played and IR_MIN_WEEKS - played < base:
            base = max(1, IR_MIN_WEEKS - played)
            reason = (f"{status}: {base} more week(s) of the {IR_MIN_WEEKS}-week minimum "
                      f"(on the list since before week 1; {played} game(s) played)")

    stated = _weeks_from_return_date(return_date, today or datetime.now(UTC).date())
    if stated is not None:
        # ESPN fills a return date for nearly every report, and for a reserve
        # list it is often just the next game: it cannot cut the minimum stint.
        if reserve and stated < base:
            return base, f"{reason}; ESPN return date {return_date} is earlier"
        return max(1, stated), f"{status}: return date {return_date}"
    n, why = _text_absence(status, s, text, base, reason)
    if news_weeks is not None and int(news_weeks) > n:
        return int(news_weeks), f"{status}: {news_reason or f'{news_weeks} weeks per the news'}"
    return n, why


def _text_absence(status: str | None, s: str, text: str, base: int,
                  reason: str) -> tuple[int, str]:
    """`expected_absence` without a return date: the report text, else the
    status base."""
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
    phrase = multi_game_phrase(text)
    if base < WEEK_TO_WEEK_GAMES and phrase:
        return WEEK_TO_WEEK_GAMES, f"{status}: {phrase} per the report"
    return base, reason


def reserve_since(history: list[dict] | None) -> date | None:
    """When the current reserve-list stint was first seen, from
    ``injury_history`` rows (newest first, as ``get_injury_history`` returns).

    The oldest row of the unbroken reserve run at the top. It is when this
    server first *recorded* the designation, never earlier than the real
    placement, so the remaining minimum it implies errs long rather than short.
    None when the latest row is not a reserve designation or has no date.
    """
    since = None
    for row in history or []:
        if not is_reserve(row.get("injury_status")):
            break
        try:
            since = datetime.fromisoformat(
                str(row.get("recorded_at")).replace("Z", "+00:00")).date()
        except ValueError:
            break
    return since


# --------------------------------------------------------------------------
# Network seams (patched in tests)
# --------------------------------------------------------------------------

async def _fetch_week_schedule(season: int, week: int) -> list[dict]:
    from .sleeper_enrichment import _fetch_week_schedule as fetch
    return await fetch(season, week, force=True)


async def _defense_rankings() -> dict:
    from .matchup_tools import get_defense_analyzer
    return await get_defense_analyzer().fetch_defense_rankings()


async def _sleeper_weeks(season: int, weeks: list[int]) -> dict[int, dict]:
    """Sleeper's projection index for each later week (cached; never raises)."""
    from .sleeper_projections import fetch_weeks
    try:
        return await fetch_weeks(season, weeks)
    except Exception as e:
        logger.debug(f"Sleeper later-week projections unavailable: {e}")
        return {}


def _rows_to_schedule(rows: list[dict]) -> dict[str, str] | None:
    """``{team: opponent}`` from fetched rows; None when too few teams to prove
    a bye — a partial response would otherwise turn every missing team into
    one (the same guard as ``week_context.week_schedule``)."""
    out: dict[str, str] = {}
    for g in rows or []:
        team, opp = normalize_team(g.get("team")), normalize_team(g.get("opponent"))
        if team and opp:
            # Both sides: a game listed once still has two teams playing.
            out[team] = opp
            out.setdefault(opp, team)
    return out if len(out) >= MIN_TEAMS_FOR_KNOWN_WEEK else None


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
        # His own volume: what he inherits from an absent starter is added
        # back only for the weeks that starter is out (`_inherited`).
        if bd.get("own_base_ppg") is not None:
            base = bd["own_base_ppg"]
        games = int(bd.get("usage_games") or 0)
        prior = base_ppg(position, bd.get("position_rank"), scoring=model)
        weight = PRIOR_GAMES / (games + PRIOR_GAMES)
        rate = regressed_rate(float(base), prior, games)
        return round(rate * usage, 2), "opportunity_regressed", round(weight, 2)
    return round(float(base) * usage, 2), source, None


def _starter_absence(entry, today: date | None = None, season_week: int | None = None) -> int:
    """Games an absent starter is expected to miss, from an ``inherited_from``
    value: a status string, or ``{status, description, return_date,
    placed_on}`` (see ``projections._absence_detail``). `season_week` applies
    the preseason-list (PUP / NFI) rule of `expected_absence` -- without it a
    teammate whose PUP minimum is served read as four more games out."""
    if not isinstance(entry, dict):
        return expected_absence(entry, today=today, season_week=season_week)[0]
    placed = entry.get("placed_on")
    if isinstance(placed, str):
        try:
            placed = date.fromisoformat(placed[:10])
        except ValueError:
            placed = None
    status = entry.get("status")
    if not is_reserve(status) and is_reserve(entry.get("game_status")):
        status = entry.get("game_status")
    return expected_absence(status, entry.get("description"), entry.get("return_date"),
                            today, placed_on=placed, season_week=season_week,
                            news_weeks=entry.get("news_weeks"),
                            news_reason=entry.get("news_reason"))[0]


def _inherited(proj: dict, today: date | None = None,
               week: int | None = None) -> tuple[float, int]:
    """``(points_per_game, team_games)`` inherited from absent starters.

    The weekly base includes a share of an out teammate's volume; ROS used to
    apply it to every remaining week. It lasts as long as the teammate's own
    expected absence — the longest one when several are out — counted in
    games from this week. ``(0.0, 0)`` when nothing was inherited.
    """
    bd = proj.get("breakdown") or {}
    own, base = bd.get("own_base_ppg"), bd.get("base_ppg")
    if own is None or base is None or bd.get("base_source") != "opportunity":
        return 0.0, 0
    bump = max(0.0, float(base) - float(own)) * float(bd.get("usage_mult") or 1.0)
    games = max((_starter_absence(s, today, week) for s in (bd.get("inherited_from") or {}).values()),
                default=1)
    return round(bump, 2), games


def _returning(proj: dict, per_game: float, position: str,
               model) -> tuple[float | None, int, list[dict]]:
    """``(per_game_after_return, games_until_return, teammates)``.

    The weekly base carries the weeks a returning teammate missed. From his
    expected return (the longest one when several are due back; 0 when he is
    back this week) the player keeps `projections.RETURNING_KEEP_WEIGHT` of
    `per_game` and the rest is the rate from the games they played together.
    ``(None, 0, [])`` without a returning teammate.
    """
    from .projections import RETURNING_KEEP_WEIGHT, base_ppg

    bd = proj.get("breakdown") or {}
    deflated = bd.get("deflated_base_ppg")
    due = list(bd.get("returning_teammates") or [])
    if deflated is None or not due or bd.get("base_source") != "opportunity":
        return None, 0, []
    prior = base_ppg(position, bd.get("position_rank"), scoring=model)
    kept = regressed_rate(float(deflated), prior, int(bd.get("deflated_games") or 0)) \
        * float(bd.get("usage_mult") or 1.0)
    after = RETURNING_KEEP_WEIGHT * per_game + (1 - RETURNING_KEEP_WEIGHT) * kept
    games = max(int(r.get("games_until_return") or 0) for r in due)
    return round(after, 2), games, due


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


def _sleeper_reads(indexes: dict[int, dict], model, p: dict) -> dict[int, tuple[float | None, str]]:
    """``{week: (points, status)}`` from Sleeper's later-week projections
    (`sleeper_projections.points_for`); weeks Sleeper has nothing for at all
    are left out."""
    from .sleeper_projections import points_for
    out = {}
    for w, index in indexes.items():
        if not (index or {}).get("by_id"):
            continue
        out[w] = points_for(index, model, player_id=p.get("player_id"), name=p["name"],
                            team=p["team"], position=p["position"])
    return out


def _later_week(model_points: float, read: tuple[float | None, str] | None, *,
                sleeper_mult: float, injured: bool,
                sleeper_return: int | None) -> tuple[float, str, str | None, float | None]:
    """``(points, source, reason, sleeper_points)`` for one week after this one.

    The blend of our number and Sleeper's for that week (see module doc):

    - Sleeper projects him: ``ROS_MODEL_WEIGHT`` × ours + the rest × his line
      times `sleeper_mult` (the role / backup-QB multipliers its line does not
      carry).
    - Sleeper lists him without points while he is hurt and projects him again
      in a later week (`sleeper_return`, the next week it does): zero for
      this one -- its return timeline (Josh Jacobs out 2026 wk5, back at 3
      then 11 points) is better informed than our one-week / four-week
      default.
    - Hurt and never projected again in the fetched weeks (`sleeper_return`
      None): no return date from Sleeper either; our window has priced the
      absence, so the model alone.
    - Healthy and listed without points (a backup): Sleeper's explicit zero
      takes its share, ours keeps ``ROS_MODEL_WEIGHT`` -- unlike this week,
      where it is zero outright, a role weeks away can still change.
    - No row at all: the model alone.
    """
    points, status = read if read else (None, "missing")
    if status == "projected" and points is not None:
        theirs = points * sleeper_mult
        mixed = ROS_MODEL_WEIGHT * model_points + (1 - ROS_MODEL_WEIGHT) * theirs
        return round(max(0.0, mixed), 2), "sleeper_blend", None, round(theirs, 2)
    if status == "not_projected":
        if injured and sleeper_return is None:
            return round(model_points, 2), "model", None, 0.0
        if injured:
            return 0.0, "sleeper_absence", \
                f"not projected by Sleeper until week {sleeper_return}", 0.0
        return round(ROS_MODEL_WEIGHT * model_points, 2), "sleeper_blend", None, 0.0
    return round(model_points, 2), "model", None, None


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
    from . import news_signals, projections
    from .projections import availability
    from .scoring import resolve_scoring

    model = resolve_scoring(scoring)
    if db is not None:
        # The weekly engine prices teammates' injuries and the QB depth chart
        # from its own handle; a ROS call that came first after a restart left
        # it without one. Hand it ours (not via project_players' `db`, which
        # would also file the whole league pool in the retro log).
        projections.get_projection_engine(db)
    windows = season_windows(settings, week)
    weeks = sorted(set(windows["regular"]) | set(windows["playoff"]))
    if not weeks:
        return {"players": [], "windows": windows, "schedule_unknown_weeks": [],
                "sleeper_ros": {"active": False, "weeks": [], "weeks_missing": [],
                                "model_weight": ROS_MODEL_WEIGHT}}
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
    # Sleeper's projection for each later week, fetched in parallel alongside
    # nothing else of ours (cached per week, `FUTURE_CACHE_TTL`).
    sleeper_weeks = [w for w in weeks if week < w <= week + ROS_SLEEPER_WEEKS_AHEAD]
    sleeper_idx = await _sleeper_weeks(season, sleeper_weeks) if sleeper_weeks else {}
    sleeper_live = sorted(w for w, i in sleeper_idx.items() if (i or {}).get("by_id"))

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
        # The injury tables and the week's practice report, as start/sit and
        # project_players read them, so this week's number is the same one
        # every other tool shows (without them a questionable DNP projected
        # here at 0.9 of his points and at 0.65 in the lineup).
        res = await projections.project_players(
            projections._with_injuries(_inputs(w, subset), db, season, w),
            scoring=scoring, num_teams=num_teams,
            superflex=superflex, season=season, week=w,
        )
        return {(r.get("player"), r.get("team")): r for r in (res or {}).get("projections") or []}

    now_proj = await _project(week, clean)
    on_bye_now = [p for p in clean
                  if (now_proj.get((p["name"], p["team"])) or {}).get("on_bye")
                  or _opponent(p["team"], week) == "BYE"]
    next_week = next((w for w in weeks if w > week), None)
    rate_proj = await _project(next_week, on_bye_now) if next_week and on_bye_now else {}

    # Kickers and defenses are priced week by week off the offense read the
    # weekly engine falls back to (`projections._unit_matchup`): a constant
    # rate made every K and every DEF identical ROS apart from byes. One
    # lookup per (position, team, opponent).
    units: dict[tuple[str, str, str], dict | None] = {}
    for p in clean:
        if p["position"] not in _DEFENSE and p["position"] != "K":
            continue
        for w in weeks:
            opponent = _opponent(p["team"], w)
            key = (p["position"], p["team"], opponent)
            if w == week or not opponent or opponent == "BYE" or key in units:
                continue
            try:
                units[key] = await projections._unit_matchup(
                    p["position"], p["team"], opponent, season, model)
            except Exception as e:
                logger.debug(f"K/DEF offense read failed for {key}: {e}")
                units[key] = None

    today = today or datetime.now(UTC).date()
    out = []
    for p in clean:
        key = (p["name"], p["team"])
        proj = now_proj.get(key) or {}
        rate_src = rate_proj.get(key) or proj
        per_game, baseline_source, prior_weight = _per_game(rate_src, p["position"], model)
        inherited, inherited_games = _inherited(rate_src, today, week)
        # A teammate due back: the inflated rate until his return, the rate
        # from their games together after it (reported as per_game).
        after, returning_games, returning = _returning(rate_src, per_game, p["position"], model)
        deflation = 0.0
        if after is not None and after < per_game:
            deflation, per_game = round(per_game - after, 2), after
        # A backup quarterback throwing to him (`qb_coupling`): the model's
        # multiplier for as long as the starter is expected out.
        qb = proj.get("qb_context") or {}
        qb_mult = float(qb.get("model_mult") or 1.0) if qb.get("applied") else 1.0
        qb_games = int(qb.get("games_out") or 0) if qb_mult < 1.0 else 0
        injury = p.get("injury") or {}
        # The news may state a length the feed lacks ("a six-week recovery",
        # "eligible to return in Week 8"): used where there is no return date.
        news_weeks, news_reason = news_signals.absence_weeks(
            proj.get("news_flags") or rate_src.get("news_flags"), today, week)
        absent, absence_reason = expected_absence(
            injury.get("status"), injury.get("description"), injury.get("return_date"), today,
            placed_on=injury.get("placed_on"), season_week=week,
            news_weeks=news_weeks, news_reason=news_reason)
        # A stated return date is a calendar date; every other window (the
        # reserve minimum, a suspension, "out 2 weeks") is games missed, so a
        # bye inside it does not use one up.
        stated = _weeks_from_return_date(injury.get("return_date"), today)
        by_calendar = stated is not None and max(1, stated) == absent
        # Sleeper's number for each later week, and what its line does not
        # carry: the role change our model found (as on this week's blend)
        # and, through the starter's absence, the backup quarterback.
        reads = _sleeper_reads(sleeper_idx, model, p) if sleeper_live else {}
        hurt = absent > 0 or availability(injury.get("status")) != "healthy"
        projected_weeks = sorted(w for w, (_, st) in reads.items() if st == "projected")
        role_mult = float(rate_src.get("role_multiplier", 1.0) or 1.0) \
            if p["position"] in _SKILL else 1.0
        qb_sleeper = float(qb.get("sleeper_mult") or 1.0) if qb_games else 1.0

        ros = playoff = 0.0
        weekly = []
        byes, injured, counted = [], [], 0
        sources: dict[str, int] = {}
        game_no = 0  # his team's games from this week on, before this one
        for w in weeks:
            opponent = _opponent(p["team"], w)
            reason = None
            source = None
            model_points = sleeper_points = None
            is_bye = opponent == "BYE" or (w == week and proj.get("on_bye"))
            missed = (w - week) if by_calendar else game_no
            if not is_bye:
                game_no += 1
            if w == week and p["team"] in played:
                points, reason = 0.0, "already played"
            elif is_bye:
                points, reason = 0.0, "bye"
                byes.append(w)
            elif missed < absent:
                points, reason = 0.0, absence_reason
                injured.append(w)
            elif w == week and proj:
                points = float(proj.get("projected_points") or 0.0)
                source = proj.get("projection_source") or "model"
            else:
                if (unit := units.get((p["position"], p["team"], opponent))) \
                        and unit.get("projected_points") is not None:
                    points = round(float(unit["projected_points"]), 2)
                    tier = unit.get("matchup_tier") or "unknown"
                    if tier not in ("unknown", "neutral"):
                        reason = f"{tier} matchup"
                    mult_qb = 1.0
                else:
                    mult, tier = _matchup(p["position"], opponent, rankings, analyzer,
                                          model.rec)
                    rate = (per_game + (inherited if game_no - 1 < inherited_games else 0.0)
                            + (deflation if game_no - 1 < returning_games else 0.0))
                    if game_no - 1 < qb_games:
                        rate *= qb_mult
                    points = round(rate * mult, 2)
                    if not opponent:
                        reason = "schedule unknown — counted as playing"
                    elif tier not in ("unknown", "neutral"):
                        reason = f"{tier} matchup"
                    mult_qb = qb_sleeper if game_no - 1 < qb_games else 1.0
                model_points = points
                if w > week:
                    points, source, why, sleeper_points = _later_week(
                        model_points, reads.get(w), sleeper_mult=role_mult * mult_qb,
                        injured=hurt,
                        sleeper_return=next((x for x in projected_weeks if x > w), None))
                    if source == "sleeper_absence":
                        injured.append(w)
                        reason = why
                else:
                    source = "model"
            if source:
                sources[source] = sources.get(source, 0) + 1
            if points > 0:
                counted += 1
            if w in windows["regular"]:
                ros += points
            if w in windows["playoff"]:
                playoff += points
            row = {"week": w, "opponent": opponent or None,
                   "points": round(points, 2), "reason": reason, "source": source}
            if model_points is not None and w > week:
                row["model_points"] = round(model_points, 2)
                row["sleeper_points"] = sleeper_points
            weekly.append(row)
        later_blend = sum(1 for r in weekly if r["week"] > week and r["source"] in (
            "sleeper_blend", "sleeper_absence"))
        later_model = sum(1 for r in weekly if r["week"] > week and r["source"] == "model")
        ros_source = ("sleeper_blend" if later_blend and not later_model
                      else "model" if later_model and not later_blend
                      else "mixed" if later_blend else "none")

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
            "baseline_source": baseline_source,
            "prior_weight": prior_weight,
            "bye_weeks": byes,
            "injury_status": injury.get("status"),
            "injury_weeks": injured,
            "injury_window": absence_reason,
            "expected_absence_games": absent,
            "weekly_points": {row["week"]: row["points"] for row in weekly},
            # Where the later weeks' numbers came from: Sleeper's projection
            # for that week blended with ours, ours alone, or a mix.
            "ros_source": ros_source,
            "week_sources": sources,
            "this_week_source": proj.get("projection_source"),
            # What the report text says about his role (`news_signals`), from
            # this week's projection (`value_trajectory` reads it too).
            "news_flags": list(proj.get("news_flags") or rate_src.get("news_flags") or []),
        }
        if absent_sleeper := [r["week"] for r in weekly if r["source"] == "sleeper_absence"]:
            # Weeks our window did not cover that Sleeper does not project
            # him for: its return is the next week it does.
            entry["sleeper_absence_weeks"] = absent_sleeper
            entry["sleeper_return_week"] = next(
                (x for x in projected_weeks if x > absent_sleeper[-1]), None)
        if qb_games:
            # The backup quarterback's multiplier on his later weeks.
            entry["qb_context"] = {k: qb.get(k) for k in (
                "starter", "starter_status", "backup", "backup_tier", "model_mult",
                "games_out", "reason")}
        if deflation:
            # Who is due back, and the per-game rate he loses from then on.
            entry["returning_teammates"] = [
                {"name": r["name"], "expected_return_week": r.get("expected_return_week"),
                 "games_until_return": r.get("games_until_return"),
                 "status": r.get("status")}
                for r in returning]
            entry["per_game_until_return"] = round(per_game + deflation, 2)
            entry["deflated_volume"] = (rate_src.get("breakdown") or {}).get("deflated_volume") or {}
            # What he has actually been producing without them -- his own
            # trailing opportunity rate before the regression toward the rank
            # prior. The market prices him on this; `value_trajectory` reads
            # the drop from it (the regressed rate hid most of it for a backup
            # with no games next to the starter: Emanuel Wilson).
            bd = rate_src.get("breakdown") or {}
            recent = bd.get("own_base_ppg") if bd.get("own_base_ppg") is not None \
                else bd.get("base_ppg")
            if recent is not None:
                entry["per_game_recent"] = round(
                    float(recent) * float(bd.get("usage_mult") or 1.0), 2)
        # What he has been producing (his trailing opportunity rate, before
        # the regression toward the rank prior, inherited volume included):
        # the market's read, which `value_trajectory` compares with the
        # blended rate of his next weeks.
        bd = rate_src.get("breakdown") or {}
        if bd.get("base_source") == "opportunity" and bd.get("base_ppg") is not None:
            entry["per_game_trailing"] = round(
                float(bd["base_ppg"]) * float(bd.get("usage_mult") or 1.0), 2)
            entry["trailing_games"] = int(bd.get("usage_games") or 0)
        if inherited:
            # A share of an absent starter's volume, priced only for his
            # expected absence (`_inherited`).
            entry["inherited_per_game"] = inherited
            entry["inherited_games"] = inherited_games
            entry["inherited_from"] = sorted(
                (rate_src.get("breakdown") or {}).get("inherited_from") or {})
        # What `value_trajectory` reads beyond the rates: the market's
        # positional rank behind the projection and a recent change of role.
        entry["market_position_rank"] = (rate_src.get("breakdown") or {}).get("position_rank")
        if rate_src.get("role_trend") in ("role_up", "role_down"):
            entry["role_trend"] = rate_src["role_trend"]
            entry["role_flags"] = list(rate_src.get("role_flags") or [])
        if include_weekly:
            entry["weekly"] = weekly
        out.append(entry)

    return {
        "players": out,
        "windows": windows,
        "schedule_unknown_weeks": [w for w in weeks if schedules.get(w) is None],
        "matchups_active": bool(rankings),
        "scoring_used": model.summary(),
        "sleeper_ros": {
            "active": bool(sleeper_live),
            "weeks": sleeper_live,
            "weeks_missing": [w for w in sleeper_weeks if w not in sleeper_live],
            "model_weight": ROS_MODEL_WEIGHT,
        },
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
            # The ESPN id `injury_history` is keyed by (see `reserve_since`).
            "report_id": report.get("player_id"),
        },
    }


def _with_reserve_dates(inputs: list[dict], db) -> list[dict]:
    """Attach ``injury.placed_on`` to reserve-list players from the recorded
    injury timeline, so the minimum stint counts from the placement."""
    if db is None or not hasattr(db, "get_injury_history"):
        return inputs
    for p in inputs:
        injury = p.get("injury") or {}
        if not is_reserve(injury.get("status")) or not injury.get("report_id"):
            continue
        try:
            injury["placed_on"] = reserve_since(
                db.get_injury_history(str(injury["report_id"]), limit=50))
        except Exception as e:
            logger.debug(f"injury history unavailable for {p.get('name')}: {e}")
    return inputs


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
    inputs = _with_reserve_dates(inputs, db)
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
    include_trajectory: bool = True,
    db=None,
) -> dict:
    """Rest-of-season and fantasy-playoff points in the league's scoring,
    with each player's value trajectory (``value_trajectory``: rising /
    falling / stable, sell_high / buy_low / hold, and why)."""
    from . import sleeper_tools
    from .database import get_shared_db
    from .lineup_tools import candidate_summary, name_candidates

    started = datetime.now(UTC)
    if not (player_names or player_ids or roster_id is not None):
        return create_success_response({
            "success": False, "players": [],
            "error": "Pass roster_id, player_ids or player_names.",
        })
    db = db if db is not None else get_shared_db()
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
    warnings: list[str] = []
    for name in player_names or []:
        # A fantasy player before a namesake: the first exact match with a
        # team used to win, and "Justin Jefferson" priced Cleveland's
        # linebacker (8.0 a week, no trajectory) instead of the receiver.
        ranked, ambiguous = name_candidates(db, name)
        pick = next((h for h in ranked if h.get("team_id")), None)
        if pick:
            ids.append(str(pick["id"]))
            if ambiguous:
                others = ", ".join(f"{c['name']} ({c['position']}, {c['team']})"
                                   for c in candidate_summary(ranked)[1:])
                warnings.append(f"{name!r} is ambiguous — priced {pick.get('full_name')} "
                                f"({(pick.get('position') or '').upper()}, "
                                f"{normalize_team(pick.get('team_id'))}); also: {others}. "
                                "Pass player_ids for an exact match.")
        else:
            unresolved.append(name)
    ids = list(dict.fromkeys(ids))

    # Every rostered player in the league as well, so each player's per-game
    # rate can be ranked against the market's rank (`value_trajectory`).
    # The projection inputs are shared, so the pool costs little extra.
    pool_ids: list[str] = []
    if include_trajectory:
        try:
            league_rosters = ((await sleeper_tools.get_rosters(league_id)) or {}).get("rosters") or []
            pool_ids = [str(p) for r in league_rosters for p in (r.get("players") or [])]
        except Exception as e:  # the pool only sharpens the market gap
            logger.debug(f"league pool unavailable for value trajectory: {e}")
    by_id, meta = await ros_for_ids(
        list(dict.fromkeys(ids + pool_ids)), league=league, season=season, week=week, db=db,
        include_weekly=include_weekly)
    if include_trajectory:
        from .value_trajectory import annotate
        annotate([by_id[i] for i in ids if i in by_id], week=week, pool=list(by_id.values()))
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
        "warnings": warnings,
        "schedule_unknown_weeks": meta["schedule_unknown_weeks"],
        "matchups_active": meta["matchups_active"],
        "scoring_used": meta["scoring_used"],
        "sleeper_ros": meta.get("sleeper_ros"),
        "elapsed_seconds": round(elapsed, 2),
        "method": (
            "this week = the weekly projection (Sleeper-first blend); each later "
            f"week = {ROS_MODEL_WEIGHT:g} × our per-game baseline (opportunity regressed "
            f"toward the rank prior, {PRIOR_GAMES} games-equivalent) × that week's "
            f"matchup multiplier + {1 - ROS_MODEL_WEIGHT:g} × Sleeper's projection for "
            "that week (our model alone where Sleeper has none: ros_source / "
            "weekly[].source); 0 on byes and inside the expected injury absence"
        ),
        "caveats": [
            "Injury return windows are estimates: the report text when it states "
            f"one, otherwise 1 week for Out and {IR_MIN_WEEKS} for IR/PUP/NFI.",
            "Later weeks carry no Vegas or weather adjustment — those lines are "
            "not published that far ahead.",
            "Sleeper's later-week projections are its current read of each week "
            "(depth chart, return timelines); they move with news like ours do.",
            "value_trajectory is where trade value is headed over the next "
            "few games (teammate returns, inherited volume ending, a return "
            "from injury, a role shift, our rank vs the market's) — timing, "
            "not a different ROS total.",
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
