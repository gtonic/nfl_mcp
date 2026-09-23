"""Week-by-week usage trends: is a player's role growing or shrinking?

Points are noisy; the role behind them is not. This lays out, per player and
per week, the opportunity numbers that predict next week better than last
week's score does, and says in which direction each one is moving:

* nflverse weekly player stats (the same file, and the same cache, as the
  opportunity projection): target share, air-yards share, WOPR, RACR, and the
  player's share of his team's carries (summed from the same file);
* Sleeper's weekly stats (keyed by Sleeper id): offensive snap share
  (``off_snp / tm_off_snp``) and red-zone opportunities (``rec_rz_tgt`` +
  ``rush_rz_att``). These are Sleeper's own counts, not an estimate; nflverse's
  weekly file has no red-zone columns at all.

Trend direction is the least-squares slope over the played weeks in the
window, times the window length — "how much did it move across the window" —
against a per-metric threshold, so a 2-point wobble in target share is
`stable` rather than a trend. Weeks on bye or not played are shown and left out
of the trend. Key-free.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import opportunity_tools
from .config import DEFAULT_TIMEOUT, create_http_client
from .errors import create_success_response, handle_http_errors, handle_validation_error
from .matchup_tools import season_cache_fresh
from .teams import normalize_team
from .week_context import resolve_season_week

logger = logging.getLogger(__name__)

SLEEPER_WEEK_STATS_URL = "https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"
USAGE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
MAX_WEEKS = 8
MAX_PLAYERS = 30

# Metric -> (label, change across the window that counts as a trend). Shares
# are in percentage points; WOPR is on its own 0-1ish scale; red-zone
# opportunities are a count per game.
TREND_METRICS: dict[str, tuple[str, float]] = {
    "target_share": ("target share", 3.0),
    "air_yards_share": ("air-yards share", 5.0),
    "wopr": ("WOPR", 0.05),
    "snap_share": ("snap share", 5.0),
    "carries_share": ("carries share", 5.0),
    "rz_opportunities": ("red-zone opportunities", 1.0),
}
_RECEIVING_METRICS = frozenset({"target_share", "air_yards_share", "wopr"})
# Below this share of the offense's snaps a player is a part-timer, whatever
# his per-snap numbers say.
PART_TIME_SNAP_SHARE = 50.0

# (season, week) -> (fetched_at, {sleeper_id: stats}).
_week_stats_cache: dict[tuple[int, int], tuple[datetime, dict]] = {}


async def _fetch_week_stats(season: int, week: int) -> dict[str, dict]:
    """Sleeper's weekly stat lines for one week, ``{}`` when unavailable."""
    key = (season, week)
    cached = _week_stats_cache.get(key)
    if cached and season_cache_fresh(season, cached[0]):
        return cached[1]
    try:
        async with create_http_client(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(SLEEPER_WEEK_STATS_URL.format(season=season, week=week))
            if resp.status_code != 200:
                return {}
            data = resp.json() or {}
    except Exception as e:
        logger.debug(f"Sleeper weekly stats fetch failed for {season} wk{week}: {e}")
        return {}
    if not isinstance(data, dict):
        return {}
    stats = {str(pid): s for pid, s in data.items() if isinstance(s, dict)}
    _week_stats_cache[key] = (datetime.now(UTC), stats)
    return stats


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100.0, 1)


def team_week_carries(logs: dict[str, dict]) -> dict[tuple[str, int], float]:
    """``{(team, week): carries}`` summed over every player in the logs."""
    totals: dict[tuple[str, int], float] = {}
    for entry in logs.values():
        for g in entry["games"]:
            key = (g.get("team") or entry["team"], g["week"])
            totals[key] = totals.get(key, 0.0) + float(g.get("carries") or 0.0)
    return totals


def teams_with_games(logs: dict[str, dict]) -> dict[int, set[str]]:
    """``{week: teams that played}``, from who shows up in the weekly file."""
    out: dict[int, set[str]] = {}
    for entry in logs.values():
        for g in entry["games"]:
            out.setdefault(g["week"], set()).add(g.get("team") or entry["team"])
    return out


def week_row(
    week: int,
    game: dict | None,
    sleeper: dict | None,
    team: str,
    team_carries: dict[tuple[str, int], float],
    played_teams: set[str] | None,
) -> dict:
    """One player-week of usage. Pure.

    `game` is his nflverse row (None when he has none that week), `sleeper` his
    Sleeper stat line. A week his team did not play is a bye; a week it did
    but he has no line is `did_not_play`.
    """
    sleeper = sleeper or {}
    team_snaps = float(sleeper.get("tm_off_snp") or 0.0)
    snaps = sleeper.get("off_snp")
    played = game is not None or bool(snaps)
    if not played:
        status = "bye" if played_teams is not None and team not in played_teams else "did_not_play"
        return {"week": week, "status": status}
    g = game or {}
    wk_team = g.get("team") or team
    carries = float(g.get("carries") or 0.0)
    team_total = team_carries.get((wk_team, week), 0.0)
    rz_targets = sleeper.get("rec_rz_tgt")
    rz_carries = sleeper.get("rush_rz_att")
    has_sleeper = bool(sleeper)
    return {
        "week": week,
        "status": "played",
        "team": wk_team,
        "opponent": g.get("opponent"),
        "targets": g.get("targets", 0.0) if game else sleeper.get("rec_tgt"),
        "target_share": _pct(g.get("target_share")),
        "air_yards": g.get("receiving_air_yards"),
        "air_yards_share": _pct(g.get("air_yards_share")),
        "wopr": None if g.get("wopr") is None else round(g["wopr"], 3),
        "racr": None if g.get("racr") is None else round(g["racr"], 2),
        "carries": carries if game else sleeper.get("rush_att"),
        "carries_share": round(carries / team_total * 100.0, 1) if game and team_total > 0 else None,
        "snap_share": (round(float(snaps or 0.0) / team_snaps * 100.0, 1)
                       if team_snaps > 0 else None),
        # Sleeper omits zero-valued keys: with a stat line present, a missing
        # red-zone key is a zero, not an unknown.
        "rz_targets": float(rz_targets or 0.0) if has_sleeper else None,
        "rz_carries": float(rz_carries or 0.0) if has_sleeper else None,
        "rz_opportunities": (float(rz_targets or 0.0) + float(rz_carries or 0.0)
                             if has_sleeper else None),
        # A quarterback's red-zone throws; not part of `rz_opportunities`,
        # which counts the touches a receiver or runner can score on.
        "rz_pass_attempts": float(sleeper.get("pass_rz_att") or 0.0) if has_sleeper else None,
    }


def _slope_change(values: list[float]) -> float:
    """Least-squares slope × (n - 1): the modelled change across the window."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mx, my = (n - 1) / 2.0, sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values, strict=True))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den * (n - 1) if den else 0.0


def _streak(values: list[float]) -> int:
    """Consecutive week-over-week moves ending at the latest value: +n up, -n down."""
    streak = 0
    for prev, cur in zip(reversed(values[:-1]), reversed(values[1:]), strict=True):
        step = 1 if cur > prev else -1 if cur < prev else 0
        if step == 0 or (streak and (step > 0) != (streak > 0)):
            break
        streak += step
    return streak


def metric_trends(rows: list[dict], position: str | None = None) -> tuple[dict[str, dict], list[str]]:
    """Per-metric average / direction over the played weeks, and flags. Pure.

    Direction is reported from two played weeks on; a rising/falling *flag*
    needs three, because two weeks move every metric one way or the other.
    A quarterback's receiving shares are always zero and are not trended.
    """
    played = [r for r in rows if r.get("status") == "played"]
    trends: dict[str, dict] = {}
    flags: list[str] = []
    for metric, (label, threshold) in TREND_METRICS.items():
        if (position or "").upper() == "QB" and metric in _RECEIVING_METRICS:
            continue
        series = [(r["week"], r[metric]) for r in played if r.get(metric) is not None]
        if not series:
            continue
        values = [v for _, v in series]
        change = _slope_change(values)
        direction = ("rising" if change >= threshold else "falling" if change <= -threshold
                     else "stable") if len(values) >= 2 else "insufficient_data"
        trends[metric] = {
            "average": round(sum(values) / len(values), 3 if metric == "wopr" else 1),
            "latest": values[-1],
            "change_over_window": round(change, 3 if metric == "wopr" else 1),
            "direction": direction,
            "weeks": len(values),
        }
        streak = _streak(values)
        if abs(streak) >= 2:
            flags.append(f"{label} {'up' if streak > 0 else 'down'} {abs(streak)} weeks in a row")
        elif direction in ("rising", "falling") and len(values) >= 3:
            flags.append(f"{label} {direction}")
    latest = played[-1] if played else None
    if latest and latest.get("snap_share") is not None and latest["snap_share"] < PART_TIME_SNAP_SHARE:
        flags.append(f"part-time role: {latest['snap_share']:.0f}% of snaps in week {latest['week']}")
    if latest and (latest.get("rz_opportunities") or 0) >= 3:
        flags.append(f"{latest['rz_opportunities']:.0f} red-zone opportunities in week {latest['week']}")
    missed = [r["week"] for r in rows if r.get("status") == "did_not_play"]
    if missed:
        flags.append("did not play week " + ", ".join(str(w) for w in missed))
    return trends, flags


async def _roster_players(league_id: str, roster_id: int, db) -> tuple[list[dict], str | None]:
    """``[{sleeper_id, name, position, team}]`` for a roster, or an error string."""
    from . import sleeper_tools
    rosters = ((await sleeper_tools.get_rosters(league_id)) or {}).get("rosters") or []
    mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if not mine:
        return [], f"No roster {roster_id} in league {league_id}"
    ids = [str(p) for p in (mine.get("players") or [])]
    athletes = db.get_athletes_by_ids(ids) if db is not None else {}
    out = []
    for pid in ids:
        row = athletes.get(pid) or {}
        out.append({
            "sleeper_id": pid,
            "name": row.get("full_name"),
            "position": (row.get("position") or "").upper(),
            "team": normalize_team(row.get("team_id")) or (row.get("team_id") or ""),
        })
    return out, None


def _sleeper_id_for(db, name: str, team: str | None, position: str | None) -> str | None:
    """Best Sleeper id for a name from the athletes cache (None when ambiguous)."""
    if db is None or not name:
        return None
    try:
        rows = db.search_athletes_by_name(name, limit=10)
    except Exception:
        return None
    target = opportunity_tools.norm_name(name)
    exact = [r for r in rows if opportunity_tools.norm_name(r.get("full_name")) == target]
    for r in exact or rows:
        if team and (normalize_team(r.get("team_id")) or "") != team:
            continue
        if position and (r.get("position") or "").upper() != position:
            continue
        return str(r.get("id"))
    return str(exact[0]["id"]) if len(exact) == 1 else None


@handle_http_errors(
    default_data={"players": []},
    operation_name="computing usage trends",
)
async def get_usage_trends(
    league_id: str | None = None,
    roster_id: int | None = None,
    player_names: list[str] | None = None,
    weeks: int = 4,
    season: int | None = None,
    through_week: int | None = None,
    db=None,
) -> dict:
    """Per-week usage (target/air-yards/carries/snap share, WOPR, red zone) with trends.

    Either `league_id` + `roster_id` (every QB/RB/WR/TE on that roster) or
    `player_names`. The window is the `weeks` completed weeks ending at
    `through_week` (default: the week before the current one).
    """
    default_data = {"players": []}
    if not isinstance(weeks, int) or not (2 <= weeks <= MAX_WEEKS):
        return handle_validation_error(f"weeks must be between 2 and {MAX_WEEKS}", default_data)
    if not player_names and not (league_id and roster_id is not None):
        return handle_validation_error(
            "Pass player_names, or league_id together with roster_id", default_data
        )
    season, current_week, inferred = await resolve_season_week(season, None)
    if season is None:
        return handle_validation_error("Could not determine the season", default_data)
    last = through_week if through_week is not None else max(1, (current_week or 2) - 1)
    window = list(range(max(1, last - weeks + 1), last + 1))

    logs = await opportunity_tools._fetch_game_logs(season)
    if not logs:
        return handle_validation_error(
            f"No nflverse weekly stats available for season {season}", default_data
        )
    name_index = opportunity_tools.build_name_index(logs)

    if player_names:
        wanted = []
        for q in player_names[:MAX_PLAYERS]:
            entry = name_index.get(opportunity_tools.norm_name(q)) or next(
                (e for e in logs.values() if opportunity_tools._match(e, q)), None)
            wanted.append({
                "query": q, "entry": entry,
                "name": (entry or {}).get("name") or q,
                "position": (entry or {}).get("position"),
                "team": (entry or {}).get("team"),
            })
        for w in wanted:
            w["sleeper_id"] = _sleeper_id_for(db, w["name"], w["team"], w["position"])
    else:
        roster, error = await _roster_players(league_id, roster_id, db)
        if error:
            return handle_validation_error(error, default_data)
        wanted = []
        for p in roster:
            if p["position"] not in USAGE_POSITIONS:
                continue  # K/DEF have no usage shares to trend
            wanted.append({**p, "entry": name_index.get(opportunity_tools.norm_name(p["name"]))})

    week_stats = {wk: await _fetch_week_stats(season, wk) for wk in window}
    carries = team_week_carries(logs)
    played_teams = teams_with_games(logs)

    players_out = []
    for w in wanted:
        entry = w.get("entry")
        games = {g["week"]: g for g in (entry or {}).get("games", [])}
        rows = [
            week_row(wk, games.get(wk), (week_stats.get(wk) or {}).get(w.get("sleeper_id") or ""),
                     w.get("team") or (entry or {}).get("team") or "", carries,
                     played_teams.get(wk))
            for wk in window
        ]
        trends, flags = metric_trends(rows, w.get("position"))
        players_out.append({
            "player": w.get("name"),
            "sleeper_id": w.get("sleeper_id"),
            "position": w.get("position"),
            "team": w.get("team"),
            "found_in_nflverse": entry is not None,
            "weeks": rows,
            "trends": trends,
            "flags": flags,
        })
    # Most-moving roles first: the ones worth reading.
    players_out.sort(key=lambda p: -len(p["flags"]))

    sleeper_weeks = [wk for wk, s in week_stats.items() if s]
    notes = []
    if len(sleeper_weeks) < len(window):
        notes.append("Sleeper weekly stats missing for week(s) "
                     + ", ".join(str(w) for w in window if w not in sleeper_weeks)
                     + " — snap share and red-zone counts are blank there.")
    return create_success_response({
        "season": season,
        "window": window,
        "week_inferred": inferred,
        "players": players_out,
        "count": len(players_out),
        "sources": {
            "shares": "nflverse weekly player stats (target_share, air_yards_share, "
                      "wopr, racr; carries share = carries / team carries)",
            "snaps_and_red_zone": "Sleeper weekly stats (off_snp / tm_off_snp; "
                                  "rec_rz_tgt + rush_rz_att) — counted, not estimated",
        },
        "trend_method": (
            "least-squares change across the played weeks of the window; "
            "rising/falling past " + ", ".join(
                f"{label} {thr:g}" for label, thr in TREND_METRICS.values())
        ),
        "notes": notes,
        "message": f"Usage trends for {len(players_out)} player(s), weeks {window[0]}-{window[-1]} of {season}.",
    })
