"""What a roster actually starts, and therefore where it is thin or deep.

Both the waiver question ("does this free agent beat anyone I start?") and the
trade question ("what do I have too much of, and who needs it?") reduce to the
same measurement: which players win a starting slot, and by how much the next
one behind them falls short. That measurement lives here once rather than in
each tool.

The lineup total is computed with the real optimizer from `win_probability`
rather than a per-position approximation, so FLEX and SUPERFLEX are filled the
way the league fills them.
"""
from __future__ import annotations

from .win_probability import _eligible, expand_slots, greedy_mean_lineup, player_mean

# Slots that hold no projectable player, or hold one who cannot be started.
_NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}
_FLEX_SLOTS = {"FLEX", "WRRB_FLEX", "REC_FLEX", "SUPER_FLEX"}
_FLEX_ELIGIBLE = ("RB", "WR", "TE")


def slot_counts(roster_positions: list[str] | None) -> dict[str, float]:
    """Starting slots per position, with FLEX spread over the positions that fill it.

    Fractional on purpose: a single flex seat is a third of a starting job for
    each of RB/WR/TE, which is what makes the replacement level reflect how deep
    a roster actually starts.
    """
    counts: dict[str, float] = {}
    flex = 0
    for raw in roster_positions or []:
        if raw in _NON_STARTING_SLOTS:
            continue
        if raw in _FLEX_SLOTS:
            flex += 1
            continue
        slot = "DEF" if raw == "DST" else raw
        counts[slot] = counts.get(slot, 0) + 1
    for position in _FLEX_ELIGIBLE:
        counts[position] = counts.get(position, 0) + flex / 3.0
    return counts


def lineup_slots(roster_positions: list[str] | None) -> dict[str, int]:
    """Whole starting slots, keyed as the lineup optimizer expects them."""
    slots: dict[str, int] = {}
    for raw in roster_positions or []:
        if raw in _NON_STARTING_SLOTS:
            continue
        slot = {"SUPER_FLEX": "SUPERFLEX", "WRRB_FLEX": "FLEX",
                "REC_FLEX": "FLEX", "DEF": "DST"}.get(raw, raw)
        slots[slot] = slots.get(slot, 0) + 1
    return slots


def replacement_levels(
    projections: list[dict], slots: dict[str, float]
) -> dict[str, float]:
    """The projection of the weakest player who still starts, per position.

    That is the bar an addition has to clear: a WR4 on a roster that starts two
    WRs changes nothing, however good he looks in isolation.
    """
    levels: dict[str, float] = {}
    by_position: dict[str, list[float]] = {}
    for p in projections:
        by_position.setdefault((p.get("position") or "").upper(), []).append(
            float(p.get("projected_points") or 0.0)
        )
    for position, points in by_position.items():
        points.sort(reverse=True)
        starters = max(1, round(slots.get(position, 1)))
        # Fewer players than slots means the slot is effectively empty, so
        # anything at all is an upgrade.
        levels[position] = points[starters - 1] if len(points) >= starters else 0.0
    return levels


def lineup_total(players: list[dict], slot_list: list[str]) -> float:
    """Projected points of the best legal starting lineup from these players."""
    assignment = greedy_mean_lineup(players, slot_list)
    return sum(player_mean(p) for p in assignment if p)


def starting_lineup_total(players: list[dict], slots: dict[str, int]) -> float:
    """`lineup_total` from whole slot counts."""
    return lineup_total(players, expand_slots(slots))


def starting_lineup(players: list[dict], slots: dict[str, int]) -> list[dict]:
    """The players who win a slot in the best legal starting lineup, FLEX included."""
    return [p for p in greedy_mean_lineup(players, expand_slots(slots)) if p]


def lineup_gain(players: list[dict], slots: dict[str, int], candidate: dict,
                base: float | None = None) -> float:
    """How much adding `candidate` improves the best legal starting lineup.

    The honest measure of an addition. A per-position bar gets FLEX wrong both
    ways: spreading two FLEX seats over RB/WR/TE rounded a roster's *second*
    TE into a starter, so a free-agent TE was scored against a player who never
    starts; ignoring FLEX entirely called a real upgrade "depth". Pass `base`
    (the roster's own total) when scoring many candidates against one roster.
    """
    if base is None:
        base = starting_lineup_total(players, slots)
    return round(starting_lineup_total([*players, candidate], slots) - base, 2)


def lineup_bars(players: list[dict], slots: dict[str, int]) -> dict[str, float]:
    """Per position, the weakest starter a new player at it would have to beat.

    Read off the real lineup: the lowest-projected player sitting in any slot
    the position can fill (its own slot or a FLEX), or 0 when such a slot is
    empty. `replacement_levels` spreads FLEX fractionally and rounds, which can
    make a roster's never-starting TE2 the bar.
    """
    slot_list = expand_slots(slots)
    assignment = greedy_mean_lineup(players, slot_list)
    bars: dict[str, float] = {}
    positions = {_position_of(slot) for slot in slot_list} - {"FLEX", "SUPERFLEX"}
    for position in positions | set(_FLEX_ELIGIBLE):
        held = [
            player_mean(p) if p else 0.0
            for slot, p in zip(slot_list, assignment, strict=True)
            if _eligible(slot, position)
        ]
        if held:
            bars[position] = min(held)
    return bars


def _position_of(slot: str) -> str:
    return "DEF" if slot == "DST" else slot


def surplus_players(
    projections: list[dict], slots: dict[str, float], margin: float = 0.0
) -> list[dict]:
    """Players who do not win a starting slot but would elsewhere.

    These are the tradeable ones: moving a player who is already starting costs
    the lineup directly, and moving one nobody would want buys nothing.
    """
    surplus: list[dict] = []
    by_position: dict[str, list[dict]] = {}
    for p in projections:
        by_position.setdefault((p.get("position") or "").upper(), []).append(p)

    for position, players in by_position.items():
        players.sort(key=lambda p: float(p.get("projected_points") or 0.0), reverse=True)
        starters = max(1, round(slots.get(position, 1)))
        for player in players[starters:]:
            if float(player.get("projected_points") or 0.0) > margin:
                surplus.append(player)
    return surplus
