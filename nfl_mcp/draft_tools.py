"""
Draft assistant tools.

Turns the consensus value layer (see player_values.py) into an actual drafting
edge:

- get_draft_board: a format-aware, tiered board ranked by Value-Based Drafting
  (VBD = value over positional replacement level), which is what actually wins
  drafts — not raw ADP.
- recommend_draft_pick: a LIVE in-draft assistant. It reads the current Sleeper
  draft state (who's already gone), models your roster construction and starter
  needs, detects positional runs and value cliffs, and tells you the best picks
  right now with reasoning.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .player_values import get_values_service, scoring_to_ppr
from .sleeper_tools import get_draft, get_draft_picks
from .errors import (
    create_success_response,
    create_error_response,
    ErrorType,
    handle_http_errors,
)

logger = logging.getLogger(__name__)

VBD_POSITIONS = ["QB", "RB", "WR", "TE"]


def replacement_baselines(num_teams: int, superflex: bool) -> Dict[str, int]:
    """Number of startable players per position across the league (VBD baseline).

    The player just past this count defines "replacement level" for the position.
    Accounts for FLEX by inflating RB/WR/TE slightly.
    """
    n = max(int(num_teams or 12), 2)
    return {
        "QB": n * (2 if superflex else 1),
        "RB": round(n * 2.5),
        "WR": round(n * 3.0),
        "TE": round(n * 1.2),
    }


def compute_vbd(values: List[Dict], num_teams: int, superflex: bool) -> Dict[str, Any]:
    """Attach VBD (value over replacement) to each player and return metadata.

    Returns {"players": [...augmented...], "replacement": {pos: value}}.
    """
    baselines = replacement_baselines(num_teams, superflex)
    by_pos: Dict[str, List[Dict]] = {}
    for v in values:
        pos = (v.get("position") or "").upper()
        if pos in VBD_POSITIONS and v.get("value") is not None:
            by_pos.setdefault(pos, []).append(v)

    replacement: Dict[str, float] = {}
    for pos, plist in by_pos.items():
        plist.sort(key=lambda x: x.get("value") or 0, reverse=True)
        baseline_idx = baselines.get(pos, len(plist)) - 1
        if plist:
            idx = min(max(baseline_idx, 0), len(plist) - 1)
            replacement[pos] = float(plist[idx].get("value") or 0)

    augmented = []
    for v in values:
        pos = (v.get("position") or "").upper()
        val = v.get("value")
        vbd = None
        if val is not None and pos in replacement:
            vbd = round(float(val) - replacement[pos], 1)
        item = dict(v)
        item["vbd"] = vbd
        augmented.append(item)

    # Best-first by VBD (players without VBD sink to the bottom).
    augmented.sort(key=lambda x: (x["vbd"] is not None, x["vbd"] if x["vbd"] is not None else -1e9), reverse=True)
    return {"players": augmented, "replacement": replacement, "baselines": baselines}


def _tier_breaks(players_at_pos: List[Dict]) -> List[Dict]:
    """Group a position's players into tiers using FantasyCalc's tier field."""
    tiers: Dict[int, List[str]] = {}
    for p in players_at_pos:
        t = p.get("tier")
        if t is None:
            continue
        tiers.setdefault(t, []).append(p.get("name"))
    return [{"tier": t, "players": names} for t, names in sorted(tiers.items())]


# ==========================================================================
# get_draft_board
# ==========================================================================

@handle_http_errors(
    default_data={"board": [], "total": 0},
    operation_name="building draft board",
)
async def get_draft_board(
    scoring: str = "ppr",
    superflex: bool = False,
    num_teams: int = 12,
    dynasty: bool = False,
    position: Optional[str] = None,
    limit: Optional[int] = 60,
    db=None,
) -> Dict[str, Any]:
    """Build a tiered, VBD-ranked draft board (consensus values).

    Ranked by Value-Based Drafting (value over positional replacement), the
    ordering that actually wins drafts. Each player shows consensus value,
    overall/position rank, tier and VBD.

    Args:
        scoring: "ppr", "half-ppr", "standard".
        superflex: True for 2-QB / superflex leagues.
        num_teams: League size (default 12).
        dynasty: Dynasty values vs redraft.
        position: Optional filter (QB, RB, WR, TE).
        limit: Max players on the board (default 60).

    Returns: {board: [...], tiers_by_position, format, source, stale}
    """
    ppr = scoring_to_ppr(scoring)
    num_qbs = 2 if superflex else 1
    service = get_values_service(db)
    data = await service.get_values(ppr, num_qbs, num_teams, dynasty)
    values = data.get("list", [])
    if not values:
        return create_error_response(
            "No player values available (value API unreachable and no cache)",
            ErrorType.HTTP,
            {"board": [], "total": 0, "source": data.get("source")},
        )

    vbd = compute_vbd(values, num_teams, superflex)
    board = vbd["players"]

    # Tiers per position (computed before filtering so they're complete).
    tiers_by_position: Dict[str, List[Dict]] = {}
    for pos in VBD_POSITIONS:
        at_pos = [p for p in values if (p.get("position") or "").upper() == pos]
        at_pos.sort(key=lambda x: x.get("overall_rank") or 1e9)
        tiers_by_position[pos] = _tier_breaks(at_pos)

    if position:
        pos = position.upper()
        board = [p for p in board if (p.get("position") or "").upper() == pos]
    if limit:
        board = board[: int(limit)]

    return create_success_response({
        "board": [{
            "player_id": p.get("player_id"),
            "name": p.get("name"),
            "position": p.get("position"),
            "team": p.get("team"),
            "value": p.get("value"),
            "vbd": p.get("vbd"),
            "overall_rank": p.get("overall_rank"),
            "position_rank": p.get("position_rank"),
            "tier": p.get("tier"),
            "trend_30day": p.get("trend_30day"),
        } for p in board],
        "total": len(board),
        "tiers_by_position": tiers_by_position,
        "replacement_values": vbd["replacement"],
        "format": {"scoring": scoring, "superflex": superflex, "num_teams": num_teams, "dynasty": dynasty},
        "source": data.get("source"),
        "stale": data.get("stale", False),
        "message": (
            f"Draft board: {len(board)} players ranked by VBD ({data.get('source')})"
            + (" ⚠️ STALE" if data.get("stale") else "")
        ),
    })


# ==========================================================================
# recommend_draft_pick (live)
# ==========================================================================

def _starter_requirements(settings: Dict) -> Dict[str, int]:
    """Extract starter slot counts from Sleeper draft settings."""
    s = settings or {}
    def g(k):
        try:
            return int(s.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0
    flex = g("slots_flex") + g("slots_wrrb_flex") + g("slots_rb_wr") + g("slots_rb_wr_te")
    return {
        "QB": g("slots_qb") + g("slots_super_flex"),
        "RB": g("slots_rb"),
        "WR": g("slots_wr"),
        "TE": g("slots_te"),
        "FLEX": flex,
    }


def _need_multiplier(pos: str, my_counts: Dict[str, int], reqs: Dict[str, int], flex_filled: int) -> Tuple[float, str]:
    """Weight a position by how badly the roster still needs it."""
    pos = pos.upper()
    if pos not in VBD_POSITIONS:
        return 1.0, "neutral"
    have = my_counts.get(pos, 0)
    need = reqs.get(pos, 0)
    if have < need:
        return 1.20, "need_starter"
    # Flex-eligible positions with an open flex slot
    if pos in ("RB", "WR", "TE") and flex_filled < reqs.get("FLEX", 0):
        return 1.08, "fills_flex"
    if have >= need + 2:
        return 0.85, "overfilled"
    return 1.0, "depth"


def _scoring_from_draft(draft: Dict) -> str:
    meta = (draft or {}).get("metadata") or {}
    st = (meta.get("scoring_type") or "").lower()
    if "half" in st:
        return "half-ppr"
    if "ppr" in st:
        return "ppr"
    if st in ("std", "standard", "2qb"):
        return "standard"
    return "ppr"


@handle_http_errors(
    default_data={"suggestions": []},
    operation_name="recommending draft pick",
)
async def recommend_draft_pick(
    draft_id: str,
    my_slot: Optional[int] = None,
    num_suggestions: int = 5,
    db=None,
) -> Dict[str, Any]:
    """Recommend the best pick(s) right now in a live Sleeper draft.

    Reads the live draft (who's gone, settings, scoring), models your roster and
    starter needs, detects positional runs and value cliffs, and returns the top
    picks by need-weighted VBD with reasoning.

    Args:
        draft_id: Sleeper draft id.
        my_slot: Your draft slot (1..N). If given, picks are weighted to your
                 roster construction; otherwise pure best-available.
        num_suggestions: How many picks to return (default 5).

    Returns: {suggestions, best_available_by_position, my_roster, positional_run,
              on_the_clock, format, source, stale}
    """
    if not draft_id:
        return create_error_response("draft_id required", ErrorType.VALIDATION, {"suggestions": []})

    draft_res = await get_draft(draft_id)
    if not draft_res.get("success") or not draft_res.get("draft"):
        return create_error_response(
            f"Could not load draft {draft_id}: {draft_res.get('error')}",
            ErrorType.HTTP, {"suggestions": []},
        )
    draft = draft_res["draft"]
    settings = draft.get("settings") or {}
    reqs = _starter_requirements(settings)
    num_teams = int(settings.get("teams", 12) or 12)
    superflex = int(settings.get("slots_super_flex", 0) or 0) > 0 or int(settings.get("slots_qb", 1) or 1) >= 2
    scoring = _scoring_from_draft(draft)
    dynasty = (draft.get("type") == "dynasty") or ((draft.get("metadata") or {}).get("is_dynasty") in (True, "true"))

    picks_res = await get_draft_picks(draft_id)
    picks = picks_res.get("picks", []) if picks_res.get("success") else []

    drafted_ids = set()
    my_counts: Dict[str, int] = {}
    my_players: List[Dict] = []
    for pk in picks:
        pid = pk.get("player_id")
        if pid:
            drafted_ids.add(str(pid))
        if my_slot is not None and pk.get("draft_slot") == my_slot:
            meta = pk.get("metadata") or {}
            pos = (meta.get("position") or "").upper()
            if pos:
                my_counts[pos] = my_counts.get(pos, 0) + 1
            my_players.append({
                "player_id": pid,
                "name": (f"{meta.get('first_name','')} {meta.get('last_name','')}".strip() or None),
                "position": pos or None,
                "round": pk.get("round"),
            })

    flex_filled = 0  # RB/WR/TE beyond their base starter reqs count toward flex
    for pos in ("RB", "WR", "TE"):
        flex_filled += max(0, my_counts.get(pos, 0) - reqs.get(pos, 0))

    # Values + VBD for this exact format.
    service = get_values_service(db)
    data = await service.get_values(scoring_to_ppr(scoring), 2 if superflex else 1, num_teams, dynasty)
    values = data.get("list", [])
    if not values:
        return create_error_response(
            "No player values available (value API unreachable and no cache)",
            ErrorType.HTTP, {"suggestions": [], "source": data.get("source")},
        )
    vbd = compute_vbd(values, num_teams, superflex)

    available = [p for p in vbd["players"] if str(p.get("player_id")) not in drafted_ids]

    # Need-weighted scoring
    scored = []
    for p in available:
        pos = (p.get("position") or "").upper()
        base_vbd = p.get("vbd")
        if base_vbd is None:
            continue
        mult, need_label = _need_multiplier(pos, my_counts, reqs, flex_filled) if my_slot is not None else (1.0, "n/a")
        scored.append({**p, "need_weighted": round(base_vbd * mult, 1), "need_label": need_label})
    scored.sort(key=lambda x: x["need_weighted"], reverse=True)

    # Best available at each position (pure VBD)
    best_by_pos: Dict[str, Dict] = {}
    for p in available:
        pos = (p.get("position") or "").upper()
        if pos in VBD_POSITIONS and pos not in best_by_pos and p.get("vbd") is not None:
            best_by_pos[pos] = {"name": p.get("name"), "value": p.get("value"), "vbd": p.get("vbd"),
                                "position_rank": p.get("position_rank"), "tier": p.get("tier")}

    # Value-cliff detection: gap from #1 to #2 available at each position
    cliffs = {}
    avail_by_pos: Dict[str, List[Dict]] = {}
    for p in available:
        pos = (p.get("position") or "").upper()
        if pos in VBD_POSITIONS and p.get("value") is not None:
            avail_by_pos.setdefault(pos, []).append(p)
    for pos, plist in avail_by_pos.items():
        plist.sort(key=lambda x: x.get("value") or 0, reverse=True)
        if len(plist) >= 2:
            gap = (plist[0].get("value") or 0) - (plist[1].get("value") or 0)
            # Flag a cliff if the drop-off to the next guy is steep (>15%).
            if plist[0].get("value") and gap / plist[0]["value"] > 0.15:
                cliffs[pos] = {"top": plist[0].get("name"), "drop": round(gap, 0)}

    # Positional run: what went in the last ~2 rounds
    recent = picks[-(2 * num_teams):] if picks else []
    run_counts: Dict[str, int] = {}
    for pk in recent:
        pos = ((pk.get("metadata") or {}).get("position") or "").upper()
        if pos in VBD_POSITIONS:
            run_counts[pos] = run_counts.get(pos, 0) + 1
    positional_run = sorted(run_counts.items(), key=lambda x: x[1], reverse=True)

    # Build suggestions with reasoning
    suggestions = []
    for p in scored[: max(1, int(num_suggestions))]:
        pos = (p.get("position") or "").upper()
        reasons = []
        if p.get("need_label") == "need_starter":
            reasons.append(f"fills an open {pos} starter slot")
        elif p.get("need_label") == "fills_flex":
            reasons.append("fills your FLEX")
        elif p.get("need_label") == "overfilled":
            reasons.append(f"you're already deep at {pos}")
        if p.get("tier") is not None:
            reasons.append(f"{pos} tier {p.get('tier')}")
        if pos in cliffs:
            reasons.append(f"⚠️ value cliff at {pos} after him (−{cliffs[pos]['drop']:.0f})")
        run_hit = next((c for pos2, c in positional_run if pos2 == pos), 0)
        if run_hit >= max(3, num_teams // 3):
            reasons.append(f"{pos} run underway ({run_hit} recently)")
        suggestions.append({
            "player_id": p.get("player_id"),
            "name": p.get("name"),
            "position": pos,
            "team": p.get("team"),
            "value": p.get("value"),
            "vbd": p.get("vbd"),
            "need_weighted_score": p.get("need_weighted"),
            "overall_rank": p.get("overall_rank"),
            "position_rank": p.get("position_rank"),
            "tier": p.get("tier"),
            "reasoning": reasons or ["best available by value"],
        })

    top = suggestions[0] if suggestions else None
    return create_success_response({
        "suggestions": suggestions,
        "top_pick": top,
        "best_available_by_position": best_by_pos,
        "value_cliffs": cliffs,
        "positional_run": [{"position": pos, "recent_picks": c} for pos, c in positional_run],
        "my_roster": {
            "slot": my_slot,
            "players": my_players,
            "position_counts": my_counts,
            "starter_requirements": reqs,
        } if my_slot is not None else None,
        "picks_made": len(picks),
        "format": {"scoring": scoring, "superflex": superflex, "num_teams": num_teams, "dynasty": dynasty},
        "source": data.get("source"),
        "stale": data.get("stale", False),
        "message": (
            (f"Pick now: {top['name']} ({top['position']}, VBD {top['vbd']})" if top else "No available players found")
            + (" ⚠️ STALE values" if data.get("stale") else "")
        ),
    })
