"""Server-side lookups behind the start/sit and lineup MCP tools.

The start/sit tools used to ask the caller for team, position, target share,
snap share and practice status — data the server has. An assistant that does
not know them guesses, and a guessed snap share moves the decision. Here a
player can be named and the rest is looked up: team/position/player_id from the
athlete cache, last week's snap share from the usage table, injury and practice
by the optimizer itself. ``analyze_lineup`` reads the whole lineup off the
league, so no lineup dict has to be typed in at all.
"""
from __future__ import annotations

import asyncio
import logging

from .teams import normalize_team

logger = logging.getLogger(__name__)

_FANTASY = ("QB", "RB", "WR", "TE", "K", "DEF")


# Sleeper's `search_rank` for a player it does not rank (a practice-squad
# linebacker sits at 9999999); every missing rank counts as this.
UNRANKED_SEARCH_RANK = 9_999_999
_ACTIVE_STATUS = "active"


def _search_rank(row: dict) -> int:
    """Sleeper's market-wide `search_rank` from an athlete row's raw JSON."""
    import json
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    try:
        rank = int((raw if isinstance(raw, dict) else {}).get("search_rank")
                   or UNRANKED_SEARCH_RANK)
    except (TypeError, ValueError):
        rank = UNRANKED_SEARCH_RANK
    return min(rank, UNRANKED_SEARCH_RANK)


def name_candidates(db, name: str | None, team: str | None = None,
                    position: str | None = None,
                    include_free_agents: bool = False) -> tuple[list[dict], bool]:
    """``(candidates, ambiguous)``: athlete rows for a name, best match first.

    Ranked by the team/position hint, then a fantasy position with a team,
    an active status and Sleeper's market ``search_rank``; exact names only
    when there are any. `ambiguous` is True when two or more exact-name
    fantasy players on a team fit the hints (two "Mike Williams"): the first
    is still the best guess, but the caller should say so.

    `include_free_agents`: a player without an NFL team (released, unsigned)
    counts like one with a team -- a fantasy position, an active status and
    the market rank decide -- and takes part in the ambiguity check. For
    ownership questions: Tyreek Hill had no team and is still rostered in a
    league, so a lookup that preferred (or required) a team answered "free
    agent" for nobody in particular.
    """
    from .opportunity_tools import norm_name

    if db is None or not name:
        return [], False
    team = normalize_team(team) if team else None
    position = (position or "").upper() or None
    try:
        hits = db.search_athletes_by_name(name, limit=25) or []
    except Exception as e:
        logger.debug(f"athlete search failed for {name!r}: {e}")
        return [], False
    exact = [h for h in hits if norm_name(h.get("full_name")) == norm_name(name)]

    def fits(h: dict) -> bool:
        pos = (h.get("position") or "").upper()
        return ((include_free_agents or bool(h.get("team_id"))) and pos in _FANTASY
                and (not team or normalize_team(h.get("team_id")) == team)
                and (not position or pos == position))

    def score(h: dict) -> tuple:
        pos = (h.get("position") or "").upper()
        if include_free_agents:
            return (
                bool(team) and normalize_team(h.get("team_id")) == team,
                bool(position) and pos == position,
                pos in _FANTASY,
                (h.get("status") or "").lower() == _ACTIVE_STATUS,
                -_search_rank(h),
                bool(h.get("team_id")),
            )
        return (
            bool(team) and normalize_team(h.get("team_id")) == team,
            bool(position) and pos == position,
            bool(h.get("team_id")) and pos in _FANTASY,
            bool(h.get("team_id")),
            (h.get("status") or "").lower() == _ACTIVE_STATUS,
            -_search_rank(h),
        )

    ranked = sorted(exact or hits, key=score, reverse=True)
    return ranked, bool(exact) and sum(1 for h in exact if fits(h)) > 1


def candidate_summary(rows: list[dict], limit: int = 5) -> list[dict]:
    """``[{name, team, position, player_id}]`` for an ambiguity warning."""
    return [{"name": r.get("full_name"), "team": normalize_team(r.get("team_id")),
             "position": (r.get("position") or "").upper() or None,
             "player_id": str(r["id"]) if r.get("id") else None}
            for r in rows[:limit]]


def resolve_player(db, name: str | None, team: str | None = None,
                   position: str | None = None, player_id: str | None = None) -> dict:
    """``{name, position, team, player_id}`` for a named player, best effort.

    Given fields win; missing ones come from the athlete cache
    (`name_candidates`). A bare team code is that team's defense. With
    several exact-name fantasy players the result also carries
    ``ambiguous: True`` and ``candidates``.
    """
    out = {"name": name, "position": (position or "").upper() or None,
           "team": normalize_team(team) if team else None, "player_id": player_id}
    if db is None:
        return out
    if player_id and not (out["team"] and out["position"]):
        row = (db.get_athletes_by_ids([str(player_id)]) or {}).get(str(player_id)) or {}
        out["team"] = out["team"] or normalize_team(row.get("team_id"))
        out["position"] = out["position"] or (row.get("position") or "").upper() or None
        out["name"] = out["name"] or row.get("full_name")
    if out["team"] and out["position"] and out["player_id"]:
        return out
    if not name:
        return out
    code = normalize_team(name)
    if code and len(name.strip()) <= 4 and (out["position"] in (None, "DEF", "DST")):
        return {"name": code, "position": "DEF", "team": code, "player_id": player_id or code}
    ranked, ambiguous = name_candidates(db, name, out["team"], out["position"])
    if ranked:
        best = ranked[0]
        out["team"] = out["team"] or normalize_team(best.get("team_id"))
        out["position"] = out["position"] or (best.get("position") or "").upper() or None
        out["player_id"] = out["player_id"] or (str(best["id"]) if best.get("id") else None)
        out["name"] = best.get("full_name") or name
        if ambiguous:
            out["ambiguous"] = True
            out["candidates"] = candidate_summary(ranked)
    return out


def recent_snap_share(db, player_id: str | None, season: int | None, week: int | None) -> float | None:
    """The player's offensive snap share in his latest recorded week before `week`."""
    if db is None or not player_id or not season or not week:
        return None
    for w in range(int(week) - 1, max(0, int(week) - 3), -1):
        try:
            rows = db.get_usage_for_week(int(season), w) or []
        except Exception:
            return None
        row = next((r for r in rows if str(r.get("player_id")) == str(player_id)), None)
        if row and row.get("snap_share") is not None:
            return float(row["snap_share"])
    return None


# Sleeper-enrichment trend words -> the optimizer's USAGE_TREND_SCORES keys.
_TREND_WORDS = {"up": "upward", "down": "downward", "flat": "stable"}
# Below this many team targets in the sample the share is noise (a partial ingest).
_MIN_TEAM_TARGETS = 15
_team_targets_cache: dict[tuple, dict[str, float]] = {}


def clear_usage_cache() -> None:
    _team_targets_cache.clear()


def _team_week_targets(db, season: int, week: int) -> dict[str, float]:
    """``{team: targets}`` for one recorded week of ``player_usage_stats``."""
    key = (id(db), int(season), int(week))
    hit = _team_targets_cache.get(key)
    if hit is not None:
        return hit
    try:
        rows = db.get_usage_for_week(int(season), int(week)) or []
        ids = [str(r.get("player_id")) for r in rows if r.get("targets")]
        teams = db.get_athletes_by_ids(ids) if ids else {}
    except Exception as e:
        logger.debug(f"team targets unavailable for {season} week {week}: {e}")
        return {}
    totals: dict[str, float] = {}
    for r in rows:
        if not r.get("targets"):
            continue
        team = normalize_team((teams.get(str(r.get("player_id"))) or {}).get("team_id"))
        if team:
            totals[team] = totals.get(team, 0.0) + float(r["targets"])
    if totals:  # an empty week may simply not be ingested yet
        if len(_team_targets_cache) > 64:
            _team_targets_cache.clear()
        _team_targets_cache[key] = totals
    return totals


def recent_usage(db, player_id: str | None, position: str | None, team: str | None,
                 season: int | None, week: int | None) -> dict:
    """``{target_share, red_zone_opportunities, usage_trend}`` from the last 3 weeks.

    The same ``player_usage_stats`` rows the roster enrichment reads: target
    share is the player's targets over his team's in the weeks he has a row,
    red-zone opportunities the per-game RZ touches, the trend the enrichment's
    targets (else snaps) trend. Only what the data supports is returned — a
    missing key keeps the optimizer's neutral default.
    """
    out: dict = {}
    if (db is None or not player_id or not season or not week
            or (position or "").upper() not in ("RB", "WR", "TE")
            or not hasattr(db, "get_usage_weekly_breakdown")):
        return out
    try:
        weeks = db.get_usage_weekly_breakdown(str(player_id), int(season), int(week), n=3) or []
    except Exception as e:
        logger.debug(f"usage breakdown unavailable for {player_id}: {e}")
        return out
    if not isinstance(weeks, list) or not weeks:
        return out
    rz = [float(r["rz_touches"]) for r in weeks if r.get("rz_touches") is not None]
    if rz:
        out["red_zone_opportunities"] = round(sum(rz) / len(rz))
    from .sleeper_enrichment import _calculate_usage_trend
    trend = _calculate_usage_trend(weeks, "targets") or _calculate_usage_trend(weeks, "snap_share")
    if trend:
        out["usage_trend"] = _TREND_WORDS.get(trend, "stable")
    team = normalize_team(team) if team else None
    if team:
        own = team_total = 0.0
        for r in weeks:
            if r.get("targets") is None or r.get("week") is None:
                continue
            total = _team_week_targets(db, season, r["week"]).get(team)
            if total:
                own += float(r["targets"])
                team_total += total
        if team_total >= _MIN_TEAM_TARGETS:
            out["target_share"] = round(100.0 * own / team_total, 1)
    return out


def player_input(db, item, season: int | None, week: int | None) -> dict:
    """A roster-recommendation input from a name or a partial player dict."""
    if isinstance(item, str):
        item = {"name": item}
    item = dict(item or {})
    who = resolve_player(db, item.get("name") or item.get("player_name"), item.get("team"),
                         item.get("position"), item.get("player_id"))
    out = {**item, "name": who["name"] or item.get("name"), "position": who["position"],
           "team": who["team"], "player_id": who["player_id"] or item.get("player_id") or "",
           "opponent": item.get("opponent") or ""}
    usage = dict(item.get("usage") or {})
    if usage.get("snap_percentage") is None:
        snap = recent_snap_share(db, out["player_id"], season, week)
        if snap is not None:
            usage["snap_percentage"] = snap
    # Target share / red-zone work / trend: without them the optimizer's
    # target-share bonus never fired and every trend read "stable".
    for key, value in recent_usage(db, out["player_id"], out["position"], out["team"],
                                   season, week).items():
        usage.setdefault(key, value)
    if usage:
        out["usage"] = usage
    return out


async def analyze_lineup(
    league_id: str | None = None,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    lineup: dict | None = None,
    db=None,
    risk_mode: str | None = "auto",
) -> dict:
    """Grade the lineup set in the league (or a supplied `lineup` dict).

    With the league's roster, ``risk_mode`` (auto | neutral | seek_variance |
    protect_floor) also weighs the lineup against this week's opponent and the
    season's playoff odds (the ``risk`` block).
    """
    from . import lineup_optimizer_tools, sleeper_tools
    from . import risk_mode as rm
    from .errors import create_success_response
    from .lineup_slots import starting_slot_list
    from .roster_context import load_roster_players

    try:
        risk_mode = rm.normalize_risk_mode(risk_mode)
    except ValueError as e:
        return create_success_response({"success": False, "error": str(e)})
    if lineup:
        return await lineup_optimizer_tools.analyze_full_lineup(
            lineup=lineup, week=week, league_id=league_id, season=season, risk_mode=risk_mode)
    if not league_id:
        return create_success_response({"success": False,
                                        "error": "Pass league_id with roster_id or user_id (or a lineup dict)."})
    ctx = await load_roster_players(league_id, roster_id, user_id, db=db, season=season, week=week)
    if ctx["error"]:
        return create_success_response({"success": False, "error": ctx["error"]})
    season, week = ctx["season"], ctx["week"]
    league, roster = ctx["league"], ctx["roster"]
    # Season playoff odds for risk_mode=auto (cached Monte Carlo), alongside
    # the projections below.
    odds_task = (asyncio.create_task(rm.playoff_pct_for(league_id, ctx["roster_id"], db=db))
                 if risk_mode == "auto" else None)
    matchups: list[dict] = []

    # This week's set lineup: the matchup's starters when Sleeper has them (they
    # follow the week), else the roster's.
    # Raw: zipped against the slot list below, so the "0" of an empty slot
    # must keep its place or every later starter shifts one slot.
    # Only when they agree with the roster (see `set_starters`): a matchup copy
    # from before a trade listed the departed players as starters.
    starters = ctx.get("starters_raw") or ctx["starters"]
    try:
        matchups = ((await sleeper_tools.get_matchups(league_id, week)) or {}).get("matchups") or []
        mine = next((m for m in matchups if m.get("roster_id") == ctx["roster_id"]), None)
        on_roster = [*(roster.get("players") or []), *(p["player_id"] for p in ctx["players"])]
        from_matchup, source = sleeper_tools.set_starters({**roster, "players": on_roster}, mine)
        if source == "matchup":
            starters = from_matchup
    except Exception as e:
        logger.debug(f"matchup starters unavailable: {e}")

    by_id = {p["player_id"]: p for p in ctx["players"]}
    slots = starting_slot_list(league.get("roster_positions"))
    built: dict[str, list[dict]] = {}
    started: set[str] = set()
    empty_slots = []
    occupied_unknown = []
    for i, slot in enumerate(slots):
        pid = starters[i] if i < len(starters) else None
        p = by_id.get(str(pid)) if pid else None
        if not p:
            if pid and str(pid) != "0":
                # Someone holds the seat, the athlete cache just cannot place
                # him (a new signing, no team). Not an open seat: never
                # suggested for filling, and left out of the grade.
                occupied_unknown.append({"slot": slot, "player_id": str(pid)})
                continue
            # "0" or no entry at all: an open seat the optimizer should fill.
            empty_slots.append(slot)
            continue
        started.add(p["player_id"])
        built.setdefault(slot, []).append(player_input(db, {
            "name": p["name"], "position": p["position"], "team": p["team"],
            "player_id": p["player_id"], "opponent": p["opponent"]}, season, week))
    built["BENCH"] = [
        player_input(db, {"name": p["name"], "position": p["position"], "team": p["team"],
                          "player_id": p["player_id"], "opponent": p["opponent"]}, season, week)
        for p in ctx["players"] if p["player_id"] not in started
    ]
    opponent_view, opponent_roster_id = None, None
    if risk_mode != "neutral":
        opponent_view, opponent_roster_id = await _opponent_projection(
            league, league_id, ctx["roster_id"], matchups, season, week, db)
    playoff_pct = None
    if odds_task is not None:
        try:
            playoff_pct = await odds_task
        except Exception as e:  # the season view is additive
            logger.debug(f"playoff odds unavailable: {e}")
    result = await lineup_optimizer_tools.analyze_full_lineup(
        lineup=built, week=week, league_id=league_id, season=season,
        empty_slots=empty_slots,
        opponent_players=opponent_view["set_players"] if opponent_view else None,
        opponent_best_players=opponent_view["best_players"] if opponent_view else None,
        risk_mode=risk_mode, playoff_pct=playoff_pct)
    if isinstance(result, dict):
        result["opponent_roster_id"] = opponent_roster_id
        if opponent_view:
            # The risk block's P(win) is against his SET lineup; his best
            # available one, and what his set lineup costs him, beside it.
            from .opponent_lineup import explain
            result["opponent"] = {
                "basis": "set_lineup",
                "set_lineup_points": opponent_view["set_lineup_points"],
                "best_lineup_points": opponent_view["best_lineup_points"],
                "points_at_risk": opponent_view["points_at_risk"],
                "lineup_issues": opponent_view["lineup_issues"],
                "locked_players": opponent_view["locked"],
                "note": explain(opponent_view),
            }
        result["league"] = {"league_id": league_id, "name": league.get("name")}
        result["roster_id"] = ctx["roster_id"]
        result["empty_slots"] = empty_slots
        result["occupied_unknown_slots"] = occupied_unknown
        reserve_ids = [str(p) for p in (roster.get("reserve") or [])]
        try:
            rows = db.get_athletes_by_ids(reserve_ids) if db is not None and reserve_ids else {}
        except Exception:
            rows = {}
        rows = rows if isinstance(rows, dict) else {}
        result["reserve"] = [
            {"player_id": pid, "name": (rows.get(pid) or {}).get("full_name") or pid,
             "position": (rows.get(pid) or {}).get("position")}
            for pid in reserve_ids
        ]
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        warnings = list(warnings)
        if occupied_unknown:
            warnings.append(
                f"{len(occupied_unknown)} starting slot(s) hold a player the athlete cache "
                "cannot place (" + ", ".join(f"{o['slot']}: {o['player_id']}" for o in occupied_unknown)
                + ") — occupied, not graded, not offered for filling.")
        if ctx.get("roster_warning"):
            warnings.append(ctx["roster_warning"])
        if warnings:
            result["warnings"] = warnings
        result["stale"] = ctx.get("stale", False)
        result["snapshot_age_seconds"] = ctx.get("snapshot_age_seconds")
        result["snapshot_fetched_at"] = ctx.get("snapshot_fetched_at")
    return result


async def _opponent_projection(league: dict, league_id: str, roster_id: int,
                               matchups: list[dict], season, week, db) -> tuple[dict | None, int | None]:
    """``(the opponent's set-vs-best view, his roster id)`` this week.

    The view is `opponent_lineup.assess_opponent_lineup`: his set starters
    (what P(win) is computed against; a starter whose game has kicked off at
    his actual points), his best available lineup from the players he could
    still start, and the lineup issues (empty / bye / Out / Doubtful) between
    the two. ``(None, None)`` without a scheduled opponent or projections.
    Never raises: the risk block is additive.
    """
    from . import sleeper_tools
    from .game_clock import week_games
    from .lineup_slots import starting_slot_list
    from .opponent_lineup import assess_opponent_lineup, merge_locked_starters
    from .projections import project_players
    from .scoring import league_scoring
    try:
        mine = next((m for m in matchups if m.get("roster_id") == roster_id), None)
        if not mine or mine.get("matchup_id") is None:
            return None, None
        opp = next((m for m in matchups if m.get("matchup_id") == mine.get("matchup_id")
                    and m.get("roster_id") != roster_id), None)
        if not opp:
            return None, None
        opp_ctx = await load_opponent(league_id, opp["roster_id"], db, season, week)
        if not opp_ctx:
            return None, opp["roster_id"]
        opp_roster = opp_ctx["roster"]
        held = [*(opp_roster.get("players") or []), *(p["player_id"] for p in opp_ctx["players"])]
        starters, _ = sleeper_tools.set_starters({**opp_roster, "players": held}, opp)
        games = week_games(db, season, week) if db is not None else {}
        by_id = {str(p["player_id"]): p for p in opp_ctx["players"]}
        # A starter whose game has kicked off stays in the matchup (and
        # scores) even once dropped; the roster copy no longer shows him.
        matchup_starters = [str(p) for p in opp.get("starters") or []]
        unknown = [p for p in matchup_starters if p not in by_id and p != "0"]
        rows = (db.get_athletes_by_ids(unknown) if db is not None and unknown else {}) or {}
        starters, _ = merge_locked_starters(
            starters, matchup_starters,
            lambda pid: (by_id.get(pid) or {}).get("team") or (rows.get(pid) or {}).get("team_id"),
            games)
        extra_labels = {pid: {"name": (rows.get(pid) or {}).get("full_name") or pid,
                              "position": (rows.get(pid) or {}).get("position"),
                              "team": (rows.get(pid) or {}).get("team_id"),
                              "reason": "unprojectable"}
                        for pid in starters if pid in rows and pid not in by_id}
        projectable = [pid for pid, p in by_id.items() if p.get("opponent") not in ("", "BYE")]
        inputs = [player_input(db, {k: by_id[pid][k] for k in
                                    ("name", "position", "team", "player_id", "opponent")},
                               season, week)
                  for pid in projectable]
        if not inputs:
            return None, opp["roster_id"]
        projected = await project_players(
            inputs, scoring=league_scoring(league),
            num_teams=int(league.get("total_rosters") or 12), season=season, week=week)
        id_of = {(by_id[pid]["name"], by_id[pid]["team"]): pid for pid in projectable}
        candidates = {}
        for p in (projected or {}).get("projections") or []:
            pid = id_of.get((p.get("player"), p.get("team")))
            if pid is None:
                continue
            candidates[pid] = {
                "name": p["player"], "position": p["position"], "team": p["team"],
                "projected_points": p["projected_points"], "floor": p["floor"],
                "ceiling": p["ceiling"], "player_id": pid,
                **({"injury_status": p["injury_status"]} if p.get("injury_status") else {}),
                **({"gameday_status": p["gameday_status"]} if p.get("gameday_status") else {}),
            }
        labels = {pid: {"name": p.get("name"), "position": p.get("position"),
                        "team": p.get("team"),
                        "reason": "bye" if p.get("opponent") == "BYE" else "unprojectable"}
                  for pid, p in by_id.items() if pid not in candidates}
        labels.update(extra_labels)
        on_lineup = {p for p in starters if p and p != "0"}
        bench = [pid for pid in by_id if pid not in on_lineup]
        view = assess_opponent_lineup(
            starting_slot_list(league.get("roster_positions")), starters, candidates, bench,
            labels, games, opp.get("players_points") or {})
        return (view if view["set_players"] else None), opp["roster_id"]
    except Exception as e:
        logger.debug(f"opponent projection unavailable: {e}")
        return None, None


async def load_opponent(league_id: str, roster_id: int, db, season, week) -> dict | None:
    """The opponent's roster context (`load_roster_players`), or None."""
    from .roster_context import load_roster_players
    ctx = await load_roster_players(league_id, roster_id, None, db=db, season=season, week=week)
    return None if ctx.get("error") else ctx


__all__ = ["analyze_lineup", "candidate_summary", "name_candidates", "player_input", "recent_snap_share",
           "recent_usage", "resolve_player"]
