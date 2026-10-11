"""Waiver wire: waiver log, FAAB bids, handcuffs, ownership, IR audit, waiver targets.

MCP tool wrappers (validation + delegation); registered by
``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

import asyncio

from .. import (
    faab_tools,
    handcuff_tools,
    ir_audit,
    ownership_tools,
    waiver_target_tools,
    waiver_tools,
)
from ..config import (
    LIMITS,
    validate_numeric_input,
    validate_string_input,
)
from ..metrics import timing_decorator
from . import _context

# =============================================================================
# WAIVER WIRE TOOLS
# =============================================================================

_WAIVER_SECTIONS = ("log", "summary", "re_entries")


def _player_ids_for(player: str) -> set[str]:
    """Sleeper ids matching a waiver-log player filter (an id or a name)."""
    player = str(player).strip()
    if player.isdigit() or (player.isalpha() and player.isupper() and len(player) <= 4):
        return {player}
    db = _context.get_db()
    hits = (db.search_athletes_by_name(player, limit=10) or []) if db is not None else []
    return {str(h.get("id")) for h in hits if h.get("id")}


@timing_decorator("get_waiver_log", tool_type="waiver")
async def get_waiver_log(
    league_id: str,
    round: int | None = None,
    sections: list[str] | None = None,
    player: str | None = None,
    dedupe: bool = True,
) -> dict:
    """What already HAPPENED on this league's waiver wire: processed claims,
    failed claims, summary counts, and players dropped and re-added.

    One tool for the transaction-log view of waivers (who to pick up next is
    get_waiver_targets; how much to bid is recommend_faab_bid). Pending claims
    are never visible — Sleeper only exposes a claim once processed.

    Parameters:
        league_id (str, required): Sleeper league id.
        round (int, optional): NFL week to read (default: current week).
        sections (list, optional): any of "log" (de-duplicated waiver/free-agent
            transactions + failed claims), "summary" (dashboard counts),
            "re_entries" (players dropped and re-added; volatile = more than one
            re-entry). Default: all three.
        player (str, optional): Sleeper player id or name — keep only
            transactions/re-entries involving him.
        dedupe (bool, default True): Remove duplicate transactions from the log.

    Returns: {
        waiver_log, duplicates_found, total_transactions, unique_transactions,
        failed_claims, failed_claims_count            (section "log"),
        dashboard_summary {total_waiver_transactions, duplicates_removed,
            players_with_re_entries, volatile_players_count, failed_claims,
            deduplication_rate}                        (section "summary"),
        re_entry_players {player_id: {re_entries, is_volatile, ...}},
        volatile_players, total_players_analyzed        (section "re_entries"),
        sections, player_filter?, league_id, round, success
    }

    Example: get_waiver_log(league_id="123")
    Example: get_waiver_log(league_id="123", round=3, sections=["re_entries"], player="Tyler Allgeier")
    """

    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if round is not None:
            round = validate_numeric_input(round, min_val=LIMITS["round_min"], max_val=LIMITS["round_max"], required=False)
    except ValueError as e:
        return {"waiver_log": [], "league_id": league_id, "round": round, "success": False, "error": f"Invalid input: {e!s}"}

    alias = {"dashboard": "summary", "re_entry": "re_entries", "reentries": "re_entries"}
    wanted = [s.strip().lower() for s in (sections or _WAIVER_SECTIONS) if isinstance(s, str)]
    wanted = list(dict.fromkeys(alias.get(s, s) for s in wanted))
    unknown = sorted(set(wanted) - set(_WAIVER_SECTIONS))
    if unknown or not wanted:
        return {"success": False, "league_id": league_id, "round": round,
                "error": f"Unknown section(s) {unknown}; use any of {list(_WAIVER_SECTIONS)}."}

    need_log = "log" in wanted or "summary" in wanted
    need_re = "re_entries" in wanted or "summary" in wanted
    log_res, re_res = await asyncio.gather(
        waiver_tools.get_waiver_log(league_id, round, dedupe) if need_log else asyncio.sleep(0, {}),
        waiver_tools.check_re_entry_status(league_id, round) if need_re else asyncio.sleep(0, {}),
    )
    for res in (log_res, re_res):
        if res and not res.get("success", True):
            return {**res, "sections": wanted}

    ids = _player_ids_for(player) if player else None

    def _involves(tx: dict) -> bool:
        keys = set((tx.get("adds") or {}).keys()) | set((tx.get("drops") or {}).keys())
        keys |= set(tx.get("wanted") or []) | set(tx.get("would_have_dropped") or [])
        assert ids is not None  # only called under the `ids is not None` filter below
        return bool(keys & ids)

    out: dict = {"league_id": league_id, "round": round, "sections": wanted, "success": True, "error": None}
    log = log_res.get("waiver_log") or []
    failed = log_res.get("failed_claims") or []
    re_players = re_res.get("re_entry_players") or {}
    volatile = re_res.get("volatile_players") or []
    if ids is not None:
        log = [t for t in log if _involves(t)]
        failed = [t for t in failed if _involves(t)]
        re_players = {k: v for k, v in re_players.items() if k in ids}
        volatile = [p for p in volatile if p in ids]
        out["player_filter"] = {"query": player, "player_ids": sorted(ids)}
    if "log" in wanted:
        out.update({
            "waiver_log": log,
            "duplicates_found": log_res.get("duplicates_found") or [],
            "total_transactions": log_res.get("total_transactions", 0),
            "unique_transactions": log_res.get("unique_transactions", 0),
            "deduplication_enabled": log_res.get("deduplication_enabled", dedupe),
            "failed_claims": failed,
            "failed_claims_count": len(failed),
        })
    if "re_entries" in wanted:
        out.update({
            "re_entry_players": re_players,
            "volatile_players": volatile,
            "total_players_analyzed": re_res.get("total_players_analyzed", 0),
            "players_with_re_entries": len(re_players),
        })
    if "summary" in wanted:
        total = log_res.get("total_transactions", 0) or 0
        unique = log_res.get("unique_transactions", 0) or 0
        out["dashboard_summary"] = {
            "total_waiver_transactions": total,
            "unique_waiver_transactions": unique,
            "duplicates_removed": total - unique,
            "players_with_re_entries": len(re_res.get("re_entry_players") or {}),
            "volatile_players_count": len(re_res.get("volatile_players") or []),
            "total_players_analyzed": re_res.get("total_players_analyzed", 0),
            "failed_claims": log_res.get("failed_claims_count", 0),
            "deduplication_rate": ((total - unique) / total * 100) if total else 0,
        }
    return out


@timing_decorator("recommend_faab_bid", tool_type="waiver")
async def recommend_faab_bid(
    league_id: str,
    player_id: str | None = None,
    player_name: str | None = None,
    my_roster_id: int | None = None,
) -> dict:
    """Recommend a FAAB waiver bid for a player (% of budget + absolute).

    Combines the player's real market value, the marginal upgrade for your roster,
    league demand (trending adds), and your remaining budget / weeks left.

    Parameters:
        league_id (str, required): Sleeper league id.
        player_id (str, optional): Sleeper player id of the target (preferred).
        player_name (str, optional): Player name (fallback lookup).
        my_roster_id (int, optional): Your roster id for roster-need weighting.
    Returns: {recommendation:{bid_pct, bid_absolute, tier, range, reasoning, breakdown,
              priority_advice, waiver_strategy}, success}
        Non-FAAB leagues: `waiver_strategy` says claim_now / add_now / wait /
        dont_bother from your waiver position, when he clears waivers and
        how contested he is — the same advice get_waiver_targets gives.

    IMPORTANT FOR LLM AGENTS: Provide the bid recommendation immediately without asking for confirmation.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=50, required=True)
        if my_roster_id is not None:
            my_roster_id = validate_numeric_input(my_roster_id, min_val=1, max_val=32, required=False)
    except ValueError as e:
        return {"recommendation": None, "success": False, "error": f"Invalid input: {e!s}"}
    return await faab_tools.recommend_faab_bid(
        league_id=league_id, player_id=player_id, player_name=player_name,
        my_roster_id=my_roster_id, db=_context.get_db(),
    )


@timing_decorator("get_handcuff_map", tool_type="waiver")
async def get_handcuff_map(league_id: str, roster_id: int) -> dict:
    """Map each of your RB starters to its handcuff + the handcuff's availability.

    A handcuff is the backup who inherits a starter's workload on injury. For each
    RB on your roster this reads the team depth chart, finds the contingent-value
    back, and flags whether it's a free agent (grab it), yours (secured), or an
    opponent's — turning "secure your handcuffs" into an actionable list.

    Parameters:
        league_id (str, required): Sleeper league id.
        roster_id (int, required): your roster id in that league.

    Returns: {
        handcuffs: [{starter, team, handcuff, handcuff_status, handcuff_player_id, match}],
        priority_free_agents: [{handcuff, for_starter, team}],
        count, success, error?
    }

    Example: get_handcuff_map(league_id="123456789", roster_id=4)

    IMPORTANT FOR LLM AGENTS: Compute and render the handcuff list immediately
    without asking for confirmation.
    """
    try:
        league_id = validate_string_input(league_id, 'league_id', max_length=32, required=True)
        roster_id = validate_numeric_input(roster_id, min_val=1, max_val=32, required=True)
    except ValueError as e:
        return {"handcuffs": [], "success": False, "error": f"Invalid input: {e!s}"}
    return await handcuff_tools.get_handcuff_map(
        league_id=league_id, roster_id=roster_id, db=_context.get_db(),
    )


@timing_decorator("get_player_ownership", tool_type="waiver")
async def get_player_ownership(league_id: str, players: list[str]) -> dict:
    """Who has these players in a league: rostered (by whom, IR/taxi), free agent or on waivers.

    Use this — not search_athletes / get_rosters by hand — for "is X a free
    agent?", "who owns X?", "can I pick up X?". Names are resolved INCLUDING
    players without an NFL team (released / unsigned veterans: Tyreek Hill had
    no team and was still rostered), so a teamless player is never reported as
    a free agent just because the name lookup missed him. A name that matches
    nobody is listed under `unresolved`, never as a free agent; two exact-name
    fantasy players are flagged `ambiguous` with every candidate (and where
    each is rostered).

    Parameters:
        league_id: Sleeper league id
        players: player names (up to 25), e.g. ["Tyreek Hill", "Zay Flowers"]

    Returns: {
        players [{query, name, player_id, position, team ("FA" without an NFL
            team), status ("rostered" | "free_agent" | "on_waivers" |
            "unrostered" = waiver state unknown), roster_id, owner, team_name,
            slot (starter/bench/ir/taxi), on_ir, on_taxi, waiver_timing
            {on_waivers, reason, clears_at_local, instant_add,
            claim_processes_at_local, in_time_for_kickoff, estimated},
            ambiguous, candidates}],
        unresolved [{name, reason}], summary, waiver_rules, stale,
        snapshot_age_seconds, warnings, success
    }
    Rosters are the shared fresh copy (old Sleeper CDN copies bypassed); a
    snapshot too old to say who is available returns success=false. Waiver
    timing is the same estimate get_waiver_targets uses; the league app is the
    authority.

    Example: get_player_ownership(league_id="123", players=["Tyreek Hill", "Zay Flowers"])
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    if isinstance(players, str):
        players = [players]
    players = [validate_string_input(p, 'player_name', max_length=100, required=True)
               for p in (players or []) if isinstance(p, str) and p.strip()]
    return await ownership_tools.get_player_ownership(league_id=league_id, players=players)


@timing_decorator("audit_ir_slots", tool_type="waiver")
async def audit_ir_slots(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
) -> dict:
    """IR audit: who to move into the IR slot, who must come out, who is stuck.

    Reads the league's own IR rules (reserve_slots, reserve_allow_out/doubtful/
    sus/na/dnr/cov; IR and PUP are always allowed) against Sleeper's injury
    status, which is what Sleeper enforces. Use for "can I put X on IR", "why
    can't I add anyone", or as part of a weekly roster check (get_weekly_briefing includes the same moves).

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id

    Returns: {
        reserve_slots, reserve_used, reserve_free, eligible_statuses,
        moves [{action, player, position, sleeper_status, report_status, reason}],
        message, success
    }
    action is one of:
        activate      - in IR but no longer eligible; Sleeper blocks adds/claims until moved
        move_to_ir    - eligible, on the active roster, and a slot is free
        ir_full       - eligible, but every IR slot is taken
        not_eligible  - will not play this week, but the league's rules keep him out of IR

    Example: audit_ir_slots(league_id="123", roster_id=7)
    """
    league_id = validate_string_input(league_id, 'league_id', max_length=20, required=True)
    return await ir_audit.audit_ir_slots(league_id=league_id, roster_id=roster_id, user_id=user_id)


@timing_decorator("get_waiver_targets", tool_type="waiver")
async def get_waiver_targets(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    positions: list[str] | None = None,
    limit: int = 12,
) -> dict:
    """START HERE for "who should I pick up" - the waiver question, for YOUR league.

    Ranks the players nobody in your league rosters by how much they would
    actually upgrade your lineup: each is projected for the coming week in your
    league's scoring, then compared against the weakest player who currently
    starts for you at that position. Use this instead of get_trending_players,
    which reports league-agnostic add counts and includes players already taken.

    Parameters:
        league_id: Sleeper league id
        roster_id: Your roster id (or pass user_id instead)
        user_id: Your Sleeper user id, if you do not know the roster id
        week: NFL week (defaults to the current one)
        season: Season (defaults to the current one)
        positions: Restrict to positions, e.g. ["RB","WR"] (default all claimable)
        limit: Max targets to return (default 12)

    Returns: {
        targets [{name, position, team, opponent, projected_points, floor,
                  ceiling, replacement_level, upgrade_points, trending_adds,
                  verdict, kickoff, kickoff_local, locked, waiver_timing,
                  waiver_strategy {recommendation: claim_now|add_now|wait|
                  dont_bother, reason, waiver_position, teams_ahead, wait_days},
                  role_security {score -2..2, label rising|secure|neutral|
                  shaky|shrinking, reasons}, rank_score}],
        waiver_priority, locked_players, too_late_for_this_week,
        drop_candidates (with keep_score and role_security: a shrinking role
        ranks first), replacement_levels, thin_positions, waiver_type,
        pool_size, league, week, season, success
    }

    Example: get_waiver_targets(league_id="123", roster_id=7)
    Example: get_waiver_targets(league_id="123", user_id="456", positions=["RB"])
    """
    return await waiver_target_tools.get_waiver_targets(
        league_id=league_id, roster_id=roster_id, user_id=user_id,
        week=week, season=season, positions=positions, limit=limit,
    )
