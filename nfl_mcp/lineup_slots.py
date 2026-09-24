"""Which player may start in which slot, and the best lineup those rules allow.

Every lineup builder in the server — the win-probability optimizer, the roster
needs behind waivers and trades, the weekly briefing and the lineup grader —
has to agree on two things: what each Sleeper ``roster_positions`` entry accepts,
and which assignment of players to slots scores the most. Each used to carry
its own copy of the first and a greedy guess at the second. The copies drifted
(a REC_FLEX was read as a plain FLEX, so a running back was started in a
receivers-only seat) and the greedy fill lost points whenever a wide flex was
filled before a narrow one. Both now live here, once.

Slot names are canonicalised to the optimizer's vocabulary: ``SUPER_FLEX`` is
``SUPERFLEX`` and ``DEF`` is ``DST``. Player positions are canonicalised the
other way round, to Sleeper's ``DEF``.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable

# Slots that hold no player who scores this week.
NON_STARTING_SLOTS = frozenset({"BN", "BENCH", "IR", "TAXI", "RES"})

_SKILL = frozenset({"RB", "WR", "TE"})

SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DST": frozenset({"DEF"}),
    "FLEX": _SKILL,
    "WRRB_FLEX": frozenset({"WR", "RB"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPERFLEX": _SKILL | {"QB"},
    "DL": frozenset({"DL"}),
    "LB": frozenset({"LB"}),
    "DB": frozenset({"DB"}),
    "IDP_FLEX": frozenset({"DL", "LB", "DB"}),
}

# Other spellings of the same slots (Sleeper's own names, and older ones used by
# callers of these tools).
_SLOT_ALIASES = {
    "SUPER_FLEX": "SUPERFLEX",
    "SF": "SUPERFLEX",
    "OP": "SUPERFLEX",
    "DEF": "DST",
    "D/ST": "DST",
    "WRT": "FLEX",
    "RB_WR_TE": "FLEX",
    "W/R/T": "FLEX",
    "RB_WR": "WRRB_FLEX",
    "WR_RB": "WRRB_FLEX",
    "W/R": "WRRB_FLEX",
    "WR_TE": "REC_FLEX",
    "W/T": "REC_FLEX",
}

_POSITION_ALIASES = {
    "DST": "DEF",
    "D/ST": "DEF",
    "DE": "DL",
    "DT": "DL",
    "CB": "DB",
    "S": "DB",
    "SS": "DB",
    "FS": "DB",
    "ILB": "LB",
    "OLB": "LB",
    "MLB": "LB",
}

# A value large enough that filling one more slot always beats any difference
# in points: a legal full lineup comes first, the highest-scoring one second.
_FILL_BONUS = 1e4


def normalize_slot(slot: str | None) -> str:
    """Canonical slot name. "WR2"/"FLEX1" lose their counter; aliases collapse."""
    s = (slot or "").strip().upper()
    if s not in SLOT_ELIGIBILITY and s not in _SLOT_ALIASES:
        s = re.sub(r"\d+$", "", s)
    return _SLOT_ALIASES.get(s, s)


def normalize_position(position: str | None) -> str:
    """Canonical player position (``DST`` -> ``DEF``)."""
    p = (position or "").strip().upper()
    return _POSITION_ALIASES.get(p, p)


def is_known_slot(slot: str | None) -> bool:
    return normalize_slot(slot) in SLOT_ELIGIBILITY


def slot_accepts(slot: str | None, position: str | None) -> bool:
    """True when a player at `position` may be started in `slot`.

    An unknown slot falls back to an exact position match rather than to
    "anything goes".
    """
    s, p = normalize_slot(slot), normalize_position(position)
    if not p:
        return False
    allowed = SLOT_ELIGIBILITY.get(s)
    return p in allowed if allowed is not None else s == p


def starting_slots(roster_positions: Iterable[str] | None) -> dict[str, int]:
    """Count the starting slots in a Sleeper ``roster_positions`` list."""
    slots: dict[str, int] = {}
    for raw in roster_positions or []:
        slot = normalize_slot(raw)
        if not slot or slot in NON_STARTING_SLOTS:
            continue
        slots[slot] = slots.get(slot, 0) + 1
    return slots


def league_starts(roster_positions: Iterable[str] | None, position: str | None) -> bool:
    """Whether any starting slot of the league can hold a ``position`` player.

    With no ``roster_positions`` at all (league not loaded) this answers True:
    nothing is known to rule the position out.
    """
    slots = starting_slot_list(roster_positions)
    if not slots:
        return True
    return any(slot_accepts(slot, position) for slot in slots)


def starting_slot_list(roster_positions: Iterable[str] | None) -> list[str]:
    """The starting slots in the league's own order (which is Sleeper's
    ``starters`` order), canonicalised."""
    return [
        normalize_slot(raw) for raw in roster_positions or []
        if normalize_slot(raw) and normalize_slot(raw) not in NON_STARTING_SLOTS
    ]


def expand_slots(slots: dict[str, int]) -> list[str]:
    out: list[str] = []
    for slot, n in slots.items():
        out.extend([normalize_slot(slot)] * int(n))
    return out


def _projected(p: dict) -> float:
    return float(p.get("projected_points", p.get("mean", 0.0)) or 0.0)


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment of every row to a distinct column (rows <= cols).

    The classic O(n^2 m) potentials method; a lineup is a dozen slots against
    a couple of dozen players, so this is exact and effectively free.
    """
    n, m = len(cost), len(cost[0]) if cost else 0
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            row = cost[i0 - 1]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = row[j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    out = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            out[p[j] - 1] = j - 1
    return out


def optimal_lineup(
    players: list[dict],
    slot_list: list[str],
    value: Callable[[dict], float] = _projected,
    position: Callable[[dict], str | None] = lambda p: p.get("position"),
) -> list[dict | None]:
    """The highest-scoring legal assignment of players to slots.

    Returns one entry per slot, in `slot_list` order; ``None`` where no eligible
    player is left. Exact: it fills as many slots as can be filled, and among
    those lineups picks the one with the most points, however the flex slots
    are ordered. Ties go to the player listed first among equals.
    """
    if not slot_list:
        return []
    # Highest value first so ties resolve the way a greedy fill would.
    pool = sorted(players, key=value, reverse=True)
    width = max(len(pool), len(slot_list))
    cost = []
    for slot in slot_list:
        row = []
        for p in pool:
            row.append(-(_FILL_BONUS + value(p)) if slot_accepts(slot, position(p)) else 0.0)
        row.extend([0.0] * (width - len(pool)))
        cost.append(row)
    picks = _hungarian(cost)
    lineup: list[dict | None] = []
    for slot, j in zip(slot_list, picks, strict=True):
        p = pool[j] if 0 <= j < len(pool) else None
        lineup.append(p if p is not None and slot_accepts(slot, position(p)) else None)
    return lineup


def assign_to_slots(
    players: list[dict],
    slot_list: list[str],
    position: Callable[[dict], str | None] = lambda p: p.get("position"),
) -> list[dict | None] | None:
    """Seat every one of `players` in a distinct eligible slot, or ``None``.

    A feasibility check for a fixed set of starters (augmenting paths), used
    when a search changes *who* starts and needs to know whether some legal
    arrangement exists, not which one scores more.
    """
    if len(players) > len(slot_list):
        return None
    seat: list[int] = [-1] * len(slot_list)  # slot index -> player index

    def _place(i: int, seen: set[int]) -> bool:
        pos = position(players[i])
        for s, slot in enumerate(slot_list):
            if s in seen or not slot_accepts(slot, pos):
                continue
            seen.add(s)
            if seat[s] == -1 or _place(seat[s], seen):
                seat[s] = i
                return True
        return False

    for i in range(len(players)):
        if not _place(i, set()):
            return None
    return [players[i] if i >= 0 else None for i in seat]
