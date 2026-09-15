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
- simulate_draft: an OFFLINE snake-draft simulator to rehearse solo and
  repeatedly. Opponents pick by need-weighted VBD with realistic ADP noise;
  your slot picks optimally (same logic as recommend_draft_pick). Returns your
  resulting roster, a value-based standing among all teams, and (for multiple
  runs) aggregate roster structure.
"""

from __future__ import annotations

import logging
import random
import re
import time
import unicodedata
from typing import Any

from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
)
from .player_values import get_values_service, scoring_to_ppr
from .sleeper_tools import get_draft, get_draft_picks

logger = logging.getLogger(__name__)

VBD_POSITIONS = ["QB", "RB", "WR", "TE"]


def replacement_baselines(num_teams: int, superflex: bool) -> dict[str, int]:
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


# Statuses that actually cost a drafted player games. Everything else is shown
# but not priced -- see _injury_multiplier for why "Questionable" is excluded.
_INJURY_MULTIPLIERS = {
    "OUT": 0.35,
    "IR": 0.30,
    "PUP": 0.45,
    "SUSPENDED": 0.50,
    "DOUBTFUL": 0.65,
}

# Only these reach the drafter. The feed also carries "Active" rows whose note is
# routine camp chatter ("held out of Thursday's preseason opener"); surfacing
# those put a two-line block under nearly every suggestion, which is the last
# thing you want on a 90-second clock.
_NOTABLE_INJURY_STATUSES = frozenset(_INJURY_MULTIPLIERS) | {"QUESTIONABLE"}


def _norm_name(s: str | None) -> str:
    """Loose name key for matching across sources (accents, suffixes, punctuation).

    The injury feed keys on ESPN ids while the value layer keys on Sleeper ids,
    so name is the only join available.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s.lower())
    return re.sub(r"[^a-z]", "", s)


def _injury_multiplier(status: str | None) -> float:
    """Draft-value multiplier for an injury status.

    "Questionable" deliberately returns 1.0. In this feed it is a news channel,
    not a severity grade: on a live preseason board it covered Mahomes ("on
    track to start Week 1"), Tucker Kraft ("expected to be full go"), Jeanty
    ("the Raiders are counting on him") and McCaffrey (a planned camp rest) --
    all tagged Questionable with an identical severity 2 / confidence 65, and
    two of them mislabelled "Knee - ACL". Discounting on that would have pushed
    first-round talent down the board for nothing. It is surfaced to the drafter
    instead, who can read the note and judge in seconds.
    """
    return _INJURY_MULTIPLIERS.get((status or "").strip().upper(), 1.0)


def draft_currency(p: dict) -> float:
    """Cross-position comparable score used to rank players.

    Deliberately the raw consensus value, NOT VBD. FantasyCalc values are a trade
    currency that already prices positional scarcity -- that is why an elite TE
    trades above a WR with similar production. Subtracting a positional
    replacement level on top of that double-counts scarcity.

    Measured against a real 12-team half-PPR mock draft (168 picks): the TE
    baseline of n*1.2 lands on a 14th TE worth 589 while the 30th RB is worth
    1381, so every TE got a flat ~+790 VBD head start over an RB of equal market
    value. Ranking by VBD pushed TEs an average of 25 board places up and QBs 16
    down, and correlated worse with actual draft order than raw value (Spearman
    0.85 vs 0.89 across rounds 6-14, where the spread is small enough for the
    offset to dominate). The visible symptom was a roster with seven tight ends.

    VBD stays on the payload: it is still the right read for "how much do I gain
    over a freely available player at this position", just not for comparing
    across positions.
    """
    v = p.get("value")
    return float(v) if v is not None else 0.0


def compute_vbd(values: list[dict], num_teams: int, superflex: bool) -> dict[str, Any]:
    """Attach VBD (value over replacement) to each player and return metadata.

    Returns {"players": [...augmented...], "replacement": {pos: value}}.
    """
    baselines = replacement_baselines(num_teams, superflex)
    by_pos: dict[str, list[dict]] = {}
    for v in values:
        pos = (v.get("position") or "").upper()
        if pos in VBD_POSITIONS and v.get("value") is not None:
            by_pos.setdefault(pos, []).append(v)

    replacement: dict[str, float] = {}
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

    # Best-first by consensus value (see draft_currency for why not by VBD).
    # Players outside the VBD positions still sink to the bottom.
    augmented.sort(key=lambda x: (x["vbd"] is not None, draft_currency(x)), reverse=True)
    return {"players": augmented, "replacement": replacement, "baselines": baselines}


def _tier_breaks(players_at_pos: list[dict]) -> list[dict]:
    """Group a position's players into tiers using FantasyCalc's tier field."""
    tiers: dict[int, list[str]] = {}
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
    position: str | None = None,
    limit: int | None = 60,
    db=None,
) -> dict[str, Any]:
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
    tiers_by_position: dict[str, list[dict]] = {}
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
        "format": {"scoring": scoring, "ppr": scoring_to_ppr(scoring), "superflex": superflex,
                   "num_teams": num_teams, "dynasty": dynasty},
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

def _starter_requirements(settings: dict) -> dict[str, int]:
    """Extract starter slot counts from Sleeper draft settings."""
    s = settings or {}
    def g(k):
        try:
            return int(s.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0
    # Sleeper uses several names for flexible skill-position slots. Count them all
    # (verified against a real league that had slots_rec_flex, which we'd missed).
    flex = (g("slots_flex") + g("slots_wrrb_flex") + g("slots_rb_wr")
            + g("slots_rb_wr_te") + g("slots_rec_flex") + g("slots_wr_te"))
    return {
        "QB": g("slots_qb") + g("slots_super_flex"),
        "RB": g("slots_rb"),
        "WR": g("slots_wr"),
        "TE": g("slots_te"),
        "FLEX": flex,
        # DEF/K carry no consensus values, so they can never be *suggested* --
        # but they are real starter slots. Tracking them here is what lets
        # recommend_draft_pick warn about an unfillable lineup late on.
        "DEF": g("slots_def"),
        "K": g("slots_k"),
    }


def _need_multiplier(pos: str, my_counts: dict[str, int], reqs: dict[str, int], flex_filled: int) -> tuple[float, str]:
    """Weight a position by how badly the roster still needs it.

    Multipliers are deliberately decisive so roster construction actually holds:
    an unfilled starter slot should usually win over slightly-higher raw value at
    an already-filled position.
    """
    pos = pos.upper()
    if pos not in VBD_POSITIONS:
        return 1.0, "neutral"
    have = my_counts.get(pos, 0)
    need = reqs.get(pos, 0)
    if have < need:
        return 2.0, "need_starter"
    # Flex-eligible positions with an open flex slot
    if pos in ("RB", "WR", "TE") and flex_filled < reqs.get("FLEX", 0):
        return 1.25, "fills_flex"
    # A surplus QB is categorically different from a surplus RB. Without a flex
    # that accepts him he can never enter the lineup, so he is not depth at all
    # -- he is a wasted roster spot. The shared 0.5 was too soft: in a live mock
    # it still ranked a third QB (raw value 1109 -> 554) above a startable RB
    # (634 -> 317), because both sides were merely "overfilled".
    if pos == "QB" and have >= need + 1:
        return 0.15, "dead_weight"
    # RB/WR/TE surplus keeps real value: byes, injuries, flex, trade bait.
    if have >= need + 2:
        return 0.5, "overfilled"
    return 1.0, "depth"


def _unrankable_gaps(
    my_counts: dict[str, int], reqs: dict[str, int], picks_left: int
) -> list[dict]:
    """Starter slots that can never be auto-suggested and are still empty.

    DEF and K have no consensus values, so they never appear in `suggestions`.
    Without this the assistant happily recommends a fourth QB in the last round
    while the lineup has an unfillable slot -- exactly what happened in a live
    mock, where 8 of 12 defenses were gone by pick 160.
    """
    gaps = []
    for pos in ("DEF", "K"):
        need = reqs.get(pos, 0)
        have = my_counts.get(pos, 0)
        if need > have:
            gaps.append({
                "position": pos,
                "needed": need - have,
                "picks_left": picks_left,
                # Nothing left to spare: every remaining pick is spoken for.
                "urgent": picks_left <= (need - have),
            })
    return gaps


def _my_picks_remaining(picks_made: int, my_slot: int, num_teams: int, rounds: int) -> int:
    """How many picks this slot still has, snake order."""
    total = num_teams * rounds
    return sum(
        1 for pk in range(picks_made + 1, total + 1)
        if _snake_slot(pk - 1, num_teams) == my_slot
    )


# How hard the fantasy-playoff schedule may tilt a pick. Ease scores derive from
# LAST season's defence rankings, so this is a tiebreaker, not a thesis. At 0.25
# the swing reached 18.8 percentage points, enough to flip a 16.7% value gap --
# a worse player winning on a schedule guess. At 0.12 the extremes (ease 13 vs
# 88) span ~9 points, which separates near-equals and loses to any real gap.
# The louder signal is the reasoning flag, not this number.
_SOS_TILT = 0.12


async def _playoff_ease_index(season: int) -> dict[str, dict[str, float]]:
    """{position: {team: ease_score}} for the fantasy playoff weeks. Never raises."""
    try:
        from .sos_tools import get_playoff_sos

        res = await get_playoff_sos(season)
    except Exception as exc:
        logger.warning("playoff SoS unavailable for draft advice: %s", exc)
        return {}
    if not res or not res.get("success"):
        return {}
    index: dict[str, dict[str, float]] = {}
    for pos, rows in (res.get("by_position") or {}).items():
        index[pos] = {
            (r.get("team") or "").upper(): r.get("ease_score")
            for r in rows or []
            if r.get("team") and r.get("ease_score") is not None
        }
    return index


def _playoff_tilt(player: dict, ease_index: dict[str, dict[str, float]]) -> tuple[float, float | None]:
    """Multiplier from the player's team schedule in weeks 15-17."""
    pos = (player.get("position") or "").upper()
    team = (player.get("team") or "").upper()
    ease = (ease_index.get(pos) or {}).get(team)
    if ease is None:
        return 1.0, None
    return 1.0 + ((ease - 50.0) / 100.0) * _SOS_TILT, ease


# Depth charts don't move during a draft, but recommend_draft_pick is called on
# every pick -- a live watcher polls it every few seconds. Uncached, a roster
# with four RBs re-fetched four ESPN pages per call and sat at ~2.4s warm.
_DEPTH_CHART_TTL = 1800.0
_depth_chart_cache: dict[str, tuple[float, dict]] = {}


async def _cached_depth_chart(team: str) -> dict:
    now = time.monotonic()
    hit = _depth_chart_cache.get(team)
    if hit and now - hit[0] < _DEPTH_CHART_TTL:
        return hit[1]
    from . import nfl_tools

    chart = await nfl_tools.get_depth_chart(team)
    _depth_chart_cache[team] = (now, chart)
    return chart


async def _handcuff_index(my_players: list[dict], db=None) -> dict[str, str]:
    """{normalised backup name: the starter he backs up} for RBs already drafted.

    Late-round picks are lottery tickets; the ticket with the best odds is the
    back who inherits a workload you already own. Reads each relevant team's
    depth chart once. Never raises.
    """
    teams: dict[str, str] = {}  # team -> my RB's name
    for p in my_players:
        if (p.get("position") or "").upper() == "RB" and p.get("team") and p.get("name"):
            teams.setdefault(p["team"].upper(), p["name"])
    if not teams:
        return {}

    out: dict[str, str] = {}
    try:
        for team, starter in teams.items():
            chart = await _cached_depth_chart(team)
            rows = (chart or {}).get("depth_chart") or []
            rbs = next((r.get("players") or [] for r in rows
                        if (r.get("position") or "").upper() == "RB"), [])
            names = [n for n in rbs if n]
            if starter not in names:
                continue
            idx = names.index(starter)
            for backup in names[idx + 1:idx + 3]:  # the next two carry the load
                out.setdefault(_norm_name(backup), starter)
    except Exception as exc:
        logger.warning("depth charts unavailable for handcuff hints: %s", exc)
        return out
    return out


async def _injury_index(db=None) -> dict[str, dict]:
    """Map normalised player name -> injury record. Never raises.

    The draft assistant must keep working when the injury feed is down; a missing
    flag is a far smaller problem than a dead recommendation mid-pick.
    """
    try:
        from .injury_service import get_injury_reports

        injuries = await get_injury_reports(teams=None, db=db, use_cache=True)
    except Exception as exc:  # network, parse, cache -- all non-fatal here
        logger.warning("injury feed unavailable for draft advice: %s", exc)
        return {}

    index: dict[str, dict] = {}
    for inj in injuries or []:
        key = _norm_name(inj.get("player_name"))
        if not key:
            continue
        if (inj.get("injury_status") or "").strip().upper() not in _NOTABLE_INJURY_STATUSES:
            continue
        # Keep the most severe record if a name appears more than once.
        prev = index.get(key)
        if prev is None or _injury_multiplier(inj.get("injury_status")) < _injury_multiplier(
            prev.get("injury_status")
        ):
            index[key] = inj
    return index


def _injury_for(player: dict, index: dict[str, dict]) -> dict | None:
    """Look up a player's injury record, guarding against name collisions."""
    inj = index.get(_norm_name(player.get("name")))
    if not inj:
        return None
    # If both sides name a team and they disagree, it's a different player.
    pt, it = (player.get("team") or "").upper(), (inj.get("team_id") or "").upper()
    if pt and it and pt != it:
        return None
    return {
        "status": inj.get("injury_status"),
        "type": inj.get("injury_type"),
        "note": inj.get("injury_description"),
        "severity": inj.get("severity"),
        "confidence": inj.get("confidence"),
        "reported": inj.get("date_reported"),
    }


def _scoring_from_draft(draft: dict) -> str:
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
    my_slot: int | None = None,
    num_suggestions: int = 5,
    db=None,
) -> dict[str, Any]:
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
    my_counts: dict[str, int] = {}
    my_players: list[dict] = []
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
                "team": (meta.get("team") or "").upper() or None,
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
    injuries = await _injury_index(db)
    try:
        season = int(draft.get("season") or 0) or None
    except (TypeError, ValueError):
        season = None
    ease_index = await _playoff_ease_index(season) if season else {}
    handcuffs = await _handcuff_index(my_players, db) if my_slot is not None else {}

    scored = []
    for p in available:
        pos = (p.get("position") or "").upper()
        if p.get("vbd") is None:
            continue  # not a VBD position (DEF/K) -- never auto-suggested
        mult, need_label = _need_multiplier(pos, my_counts, reqs, flex_filled) if my_slot is not None else (1.0, "n/a")
        inj = _injury_for(p, injuries)
        inj_mult = _injury_multiplier(inj.get("status")) if inj else 1.0
        sos_mult, ease = _playoff_tilt(p, ease_index)
        hc_for = handcuffs.get(_norm_name(p.get("name")))
        # A handcuff is worth more to YOU than to the market, which prices him
        # for whoever owns the starter. Only applied once base starters are set,
        # so it can never distort the early rounds.
        hc_mult = 1.30 if (hc_for and need_label in ("depth", "overfilled")) else 1.0
        scored.append({
            **p,
            "need_weighted": round(draft_currency(p) * mult * inj_mult * sos_mult * hc_mult, 1),
            "need_label": need_label, "injury": inj,
            "playoff_ease": round(ease, 1) if ease is not None else None,
            "handcuff_for": hc_for,
        })
    scored.sort(key=lambda x: x["need_weighted"], reverse=True)

    # Best available at each position (pure VBD)
    best_by_pos: dict[str, dict] = {}
    for p in available:
        pos = (p.get("position") or "").upper()
        if pos in VBD_POSITIONS and pos not in best_by_pos and p.get("vbd") is not None:
            best_by_pos[pos] = {"name": p.get("name"), "value": p.get("value"), "vbd": p.get("vbd"),
                                "position_rank": p.get("position_rank"), "tier": p.get("tier")}

    # Value-cliff detection: gap from #1 to #2 available at each position
    cliffs = {}
    avail_by_pos: dict[str, list[dict]] = {}
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
    run_counts: dict[str, int] = {}
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
        elif p.get("need_label") == "dead_weight":
            reasons.append(f"⛔ he can never start for you — {pos} is full")
        elif p.get("need_label") == "overfilled":
            reasons.append(f"you're already deep at {pos}")
        if p.get("handcuff_for"):
            reasons.append(f"🔗 handcuff to your {p['handcuff_for']}")
        ease = p.get("playoff_ease")
        if ease is not None and (ease >= 65 or ease <= 38):
            word = "soft" if ease >= 65 else "brutal"
            reasons.append(f"wk15-17 schedule {word} ({ease:.0f}/100)")
        if p.get("tier") is not None:
            reasons.append(f"{pos} tier {p.get('tier')}")
        if pos in cliffs:
            reasons.append(f"⚠️ value cliff at {pos} after him (−{cliffs[pos]['drop']:.0f})")
        run_hit = next((c for pos2, c in positional_run if pos2 == pos), 0)
        if run_hit >= max(3, num_teams // 3):
            reasons.append(f"{pos} run underway ({run_hit} recently)")
        inj = p.get("injury")
        if inj and inj.get("status"):
            detail = f" ({inj['type']})" if inj.get("type") else ""
            if _injury_multiplier(inj["status"]) < 1.0:
                reasons.append(f"🚑 {inj['status']}{detail} — value discounted")
            else:
                # Flagged, not priced: the drafter reads the note and decides.
                reasons.append(f"ℹ️ {inj['status']}{detail} — check the note")
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
            "injury": p.get("injury"),
            "playoff_ease": p.get("playoff_ease"),
            "handcuff_for": p.get("handcuff_for"),
            "reasoning": reasons or ["best available by value"],
        })

    top = suggestions[0] if suggestions else None

    rounds = int(settings.get("rounds", 15) or 15)
    picks_left = (
        _my_picks_remaining(len(picks), my_slot, num_teams, rounds)
        if my_slot is not None else 0
    )
    roster_gaps = _unrankable_gaps(my_counts, reqs, picks_left) if my_slot is not None else []

    return create_success_response({
        "suggestions": suggestions,
        "top_pick": top,
        "roster_gaps": roster_gaps,
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
        "format": {"scoring": scoring, "ppr": scoring_to_ppr(scoring), "superflex": superflex,
                   "num_teams": num_teams, "dynasty": dynasty},
        "source": data.get("source"),
        "stale": data.get("stale", False),
        "message": (
            # The gap warning goes first: it is the one thing the suggestion list
            # structurally cannot tell you.
            (("; ".join(
                f"⚠️ still need {g['needed']} {g['position']} and only "
                f"{g['picks_left']} pick(s) left" if g["urgent"]
                else f"note: {g['position']} slot still empty ({g['picks_left']} picks left)"
                for g in roster_gaps) + " | ") if roster_gaps else "")
            + (f"Pick now: {top['name']} ({top['position']}, value {top['value']}, VBD {top['vbd']})"
               if top else "No available players found")
            + (" ⚠️ STALE values" if data.get("stale") else "")
        ),
    })


# ==========================================================================
# simulate_draft (offline rehearsal)
# ==========================================================================

def _flex_filled(counts: dict[str, int], reqs: dict[str, int]) -> int:
    """RB/WR/TE drafted beyond their base starter requirements (fill FLEX)."""
    return sum(max(0, counts.get(pos, 0) - reqs.get(pos, 0)) for pos in ("RB", "WR", "TE"))


# Reasonable bench depth per position (on top of starter requirements) so a
# simulated roster stops stacking one position and looks like a real draft.
POS_BENCH_ALLOW = {"QB": 1, "TE": 2, "RB": 5, "WR": 5}


def _position_caps(reqs: dict[str, int]) -> dict[str, int]:
    return {pos: reqs.get(pos, 0) + POS_BENCH_ALLOW.get(pos, 4) for pos in VBD_POSITIONS}


def _eligible_players(
    avail: list[dict], counts: dict[str, int], reqs: dict[str, int],
    caps: dict[str, int], picks_left: int,
) -> list[dict]:
    """Restrict candidates so rosters fill starters and don't over-stack.

    - Late-round guarantee: when there are just enough picks left to fill the
      remaining required starter slots, only positions that fill one are allowed.
    - Otherwise: drop positions already at their bench cap.
    """
    unfilled = {pos: max(0, reqs.get(pos, 0) - counts.get(pos, 0)) for pos in VBD_POSITIONS}
    unfilled = {pos: u for pos, u in unfilled.items() if u > 0}
    flex_need = max(0, reqs.get("FLEX", 0) - _flex_filled(counts, reqs))
    total_unfilled = sum(unfilled.values()) + flex_need

    if picks_left <= total_unfilled:
        allowed = set(unfilled.keys())
        if flex_need > 0:
            allowed |= {"RB", "WR", "TE"}
        forced = [p for p in avail if (p.get("position") or "").upper() in allowed]
        if forced:
            return forced

    under_cap = [
        p for p in avail
        if counts.get((p.get("position") or "").upper(), 0) < caps.get((p.get("position") or "").upper(), 99)
    ]
    return under_cap or avail


def _need_weighted_ranking(
    available: list[dict], counts: dict[str, int], reqs: dict[str, int]
) -> list[tuple[dict, float]]:
    """Rank available players by need-weighted value (best first) for one roster."""
    flex = _flex_filled(counts, reqs)
    scored: list[tuple[dict, float]] = []
    for p in available:
        pos = (p.get("position") or "").upper()
        score = draft_currency(p)
        if p.get("vbd") is None:
            score *= 0.001  # DEF/K: keep them behind every rankable player
        mult, _ = _need_multiplier(pos, counts, reqs, flex)
        scored.append((p, score * mult))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _snake_slot(overall_index: int, num_teams: int) -> int:
    """Return the 1-based slot picking at a 0-based overall pick index (snake)."""
    rnd = overall_index // num_teams
    pos_in_round = overall_index % num_teams
    if rnd % 2 == 0:
        return pos_in_round + 1
    return num_teams - pos_in_round


def _starting_lineup_value(players: list[dict], reqs: dict[str, int]) -> float:
    """Sum of a roster's optimal starting lineup (QB/RB/WR/TE + FLEX).

    This is what a draft is really graded on — starters, not deep bench. Uses the
    same currency as the recommender (see draft_currency); grading on VBD while
    selecting on value would score a roster against a yardstick the picks were
    never optimised for.
    """
    by_pos: dict[str, list[float]] = {}
    for p in players:
        pos = (p.get("position") or "").upper()
        if pos in VBD_POSITIONS:
            by_pos.setdefault(pos, []).append(draft_currency(p))
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    total = 0.0
    leftovers: list[float] = []  # flex-eligible players not used as base starters
    for pos in VBD_POSITIONS:
        need = reqs.get(pos, 0)
        vals = by_pos.get(pos, [])
        total += sum(vals[:need])
        if pos in ("RB", "WR", "TE"):
            leftovers.extend(vals[need:])
    leftovers.sort(reverse=True)
    total += sum(leftovers[: reqs.get("FLEX", 0)])
    return round(total, 1)


def _grade_from_value(my_value: float, field_values: list[float]) -> str:
    """Letter grade from where a team's starter value sits in the field's range.

    Distance-based (not ordinal rank): when the field is tightly bunched, being a
    hair behind shouldn't crater your grade. Grades on the fraction of the
    realized value spread captured (0 = worst roster, 1 = best roster).
    """
    if not field_values:
        return "A"
    best, worst = max(field_values), min(field_values)
    if best <= worst:
        return "A"
    pct = (my_value - worst) / (best - worst)
    if pct >= 0.80:
        return "A"
    if pct >= 0.55:
        return "B"
    if pct >= 0.30:
        return "C"
    if pct >= 0.12:
        return "D"
    return "F"


def _simulate_one(
    pool: list[dict], num_teams: int, rounds: int, my_slot: int,
    reqs: dict[str, int], randomness: float, rng: random.Random,
) -> dict[str, Any]:
    """Run one full snake draft. Returns per-slot rosters and my team's detail."""
    available = list(pool)  # already VBD-sorted; shallow copy of dict refs
    {str(p["player_id"]): p for p in available}
    drafted_ids: set = set()
    counts: dict[int, dict[str, int]] = {s: {} for s in range(1, num_teams + 1)}
    rosters: dict[int, list[dict]] = {s: [] for s in range(1, num_teams + 1)}
    sigma = max(0.01, randomness * 5.0)
    caps = _position_caps(reqs)

    total_picks = num_teams * rounds
    for overall in range(total_picks):
        slot = _snake_slot(overall, num_teams)
        rnd = overall // num_teams + 1
        avail = [p for p in available if str(p["player_id"]) not in drafted_ids]
        if not avail:
            break
        picks_left = rounds - len(rosters[slot])  # includes the current pick
        avail = _eligible_players(avail, counts[slot], reqs, caps, picks_left)
        scored = _need_weighted_ranking(avail, counts[slot], reqs)
        if slot == my_slot:
            choice = scored[0][0]  # optimal (same logic as recommend_draft_pick)
        else:
            idx = min(int(abs(rng.gauss(0, sigma))), len(scored) - 1)
            choice = scored[idx][0]
        pid = str(choice["player_id"])
        drafted_ids.add(pid)
        pos = (choice.get("position") or "").upper()
        counts[slot][pos] = counts[slot].get(pos, 0) + 1
        rosters[slot].append({
            "round": rnd,
            "pick_no": overall + 1,
            "player_id": pid,
            "name": choice.get("name"),
            "position": pos,
            "team": choice.get("team"),
            "value": choice.get("value"),
            "vbd": choice.get("vbd"),
        })

    # Grade on STARTER value (optimal starting lineup), not deep-bench totals.
    starter_vbd = {s: _starting_lineup_value(rosters[s], reqs) for s in rosters}
    team_vbd = {s: round(sum((r.get("vbd") or 0) for r in rosters[s]), 1) for s in rosters}
    standings = sorted(starter_vbd.items(), key=lambda x: x[1], reverse=True)
    my_rank = next(i + 1 for i, (s, _) in enumerate(standings) if s == my_slot)

    starters_filled = all(counts[my_slot].get(pos, 0) >= need for pos, need in reqs.items() if pos != "FLEX")

    field_vals = list(starter_vbd.values())
    best = max(field_vals)
    my_val = starter_vbd[my_slot]
    gap_to_best_pct = round((best - my_val) / best * 100, 1) if best else 0.0

    return {
        "my_team": rosters[my_slot],
        "my_position_counts": counts[my_slot],
        "my_starter_vbd": my_val,
        "my_total_vbd": team_vbd[my_slot],
        "my_total_value": round(sum((r.get("value") or 0) for r in rosters[my_slot]), 0),
        "starters_filled": starters_filled,
        "my_value_rank": my_rank,
        "gap_to_best_pct": gap_to_best_pct,
        "grade": _grade_from_value(my_val, field_vals),
        "standings": [
            {"slot": s, "starter_vbd": v, "total_vbd": team_vbd[s], "is_me": s == my_slot}
            for s, v in standings
        ],
        "rosters_by_slot": {
            s: [f"{r['name']} ({r['position']})" for r in rosters[s]] for s in rosters
        },
    }


@handle_http_errors(
    default_data={"sample": None},
    operation_name="simulating draft",
)
async def simulate_draft(
    my_slot: int,
    num_teams: int = 12,
    rounds: int = 15,
    scoring: str = "ppr",
    superflex: bool = False,
    dynasty: bool = False,
    randomness: float = 0.35,
    num_sims: int = 1,
    seed: int | None = None,
    db=None,
) -> dict[str, Any]:
    """Rehearse a full snake draft offline (solo, repeatable).

    Opponents pick by need-weighted VBD with realistic ADP noise; your slot
    picks optimally (same logic as recommend_draft_pick). Grading is based on
    your optimal STARTING lineup value (not deep bench). Note: only QB/RB/WR/TE
    are modeled (no K/DST in the consensus value set).

    Args:
        my_slot: Your draft position (1..num_teams).
        num_teams: League size (default 12).
        rounds: Number of rounds (default 15).
        scoring: "ppr", "half-ppr", "standard".
        superflex: True for 2-QB / superflex.
        dynasty: Dynasty values vs redraft.
        randomness: Opponent ADP noise 0..1 (default 0.35 ~ realistic human
            variance; lower makes opponents near-perfect so draft slot dominates,
            higher makes them erratic so disciplined play always wins).
        num_sims: How many drafts to run. >1 returns aggregate structure.
        seed: Optional RNG seed for reproducibility.

    Returns: {sample: {my_team, standings, grade, ...}, aggregate?, format, source}
    """
    if my_slot < 1 or my_slot > num_teams:
        return create_error_response(
            f"my_slot must be between 1 and num_teams ({num_teams})",
            ErrorType.VALIDATION, {"sample": None},
        )

    service = get_values_service(db)
    data = await service.get_values(scoring_to_ppr(scoring), 2 if superflex else 1, num_teams, dynasty)
    values = data.get("list", [])
    if not values:
        return create_error_response(
            "No player values available (value API unreachable and no cache)",
            ErrorType.HTTP, {"sample": None, "source": data.get("source")},
        )

    vbd = compute_vbd(values, num_teams, superflex)
    pool = vbd["players"]

    settings = {"slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1,
                "slots_flex": 1, "slots_super_flex": 1 if superflex else 0}
    reqs = _starter_requirements(settings)

    n = max(1, min(int(num_sims), 200))
    randomness = max(0.0, min(float(randomness), 1.0))

    sims: list[dict[str, Any]] = []
    for i in range(n):
        rng = random.Random((seed + i) if seed is not None else None)
        sims.append(_simulate_one(pool, num_teams, rounds, my_slot, reqs, randomness, rng))

    sample = sims[0]

    result: dict[str, Any] = {
        "sample": sample,
        "format": {"scoring": scoring, "ppr": scoring_to_ppr(scoring), "superflex": superflex,
                   "num_teams": num_teams, "dynasty": dynasty, "rounds": rounds},
        "starter_requirements": reqs,
        "num_sims": n,
        "source": data.get("source"),
        "stale": data.get("stale", False),
    }

    if n > 1:
        # Aggregate my roster structure across sims.
        pos_totals: dict[str, float] = {}
        starter_vbd_sum = 0.0
        rank_sum = 0.0
        grade_counts: dict[str, int] = {}
        for s in sims:
            for pos, c in s["my_position_counts"].items():
                pos_totals[pos] = pos_totals.get(pos, 0) + c
            starter_vbd_sum += s["my_starter_vbd"]
            rank_sum += s["my_value_rank"]
            grade_counts[s["grade"]] = grade_counts.get(s["grade"], 0) + 1
        result["aggregate"] = {
            "avg_position_counts": {pos: round(t / n, 2) for pos, t in pos_totals.items()},
            "avg_starter_vbd": round(starter_vbd_sum / n, 1),
            "avg_value_rank": round(rank_sum / n, 2),
            "grade_distribution": grade_counts,
        }
        result["message"] = (
            f"{n} sims from slot {my_slot}: avg value-rank {result['aggregate']['avg_value_rank']} "
            f"of {num_teams}, grades {grade_counts}"
        )
    else:
        result["message"] = (
            f"Mock from slot {my_slot}: grade {sample['grade']} "
            f"(value-rank {sample['my_value_rank']}/{num_teams}, "
            f"{sample['gap_to_best_pct']}% behind best), "
            f"starters {'filled' if sample['starters_filled'] else 'INCOMPLETE'}"
        )

    return create_success_response(result)
