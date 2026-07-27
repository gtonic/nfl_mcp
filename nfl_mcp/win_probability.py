"""Win-probability lineup optimization.

Season-long fantasy is decided by P(win), not E[points]. Against a strong
opponent you want *ceiling* (variance helps you catch up); against a weak one you
want *floor* (variance can only cost you). This module optimizes the lineup that
maximizes the probability of outscoring a *specific* opponent — which naturally
tilts ceiling-first when you're the underdog and floor-first when you're favored.

Each player is a Normal(mean, sd) where mean is the projection and sd comes from
the floor/ceiling band (or a position volatility fallback). Team score is the sum
(treated as independent — QB/WR stacking correlation is a future refinement), so

    P(win) = Φ( (mean_you - mean_opp) / sqrt(var_you + var_opp) )

is exact. Lineup selection maximizes that P(win) via local search over bench
swaps, so the objective itself decides floor vs ceiling.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .errors import create_success_response, handle_http_errors, handle_validation_error
from .projections import _VOLATILITY

FLEX_ELIGIBLE = {"RB", "WR", "TE"}
SUPERFLEX_ELIGIBLE = {"QB", "RB", "WR", "TE"}
DEFAULT_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
_MAX_SWAP_ITERS = 50
# Positive correlation between a QB and a same-team pass catcher (shared game
# script): a stack widens the team's variance, which helps the underdog and
# hurts the favorite — exactly the effect stacking is prized for.
STACK_CORRELATION = 0.35


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_prob(my_mean: float, my_var: float, opp_mean: float, opp_var: float) -> float:
    """P(my score > opponent score) for two independent Normal team totals."""
    total_var = my_var + opp_var
    if total_var <= 0:
        return 1.0 if my_mean > opp_mean else 0.5 if my_mean == opp_mean else 0.0
    return _phi((my_mean - opp_mean) / math.sqrt(total_var))


def player_mean(p: Dict) -> float:
    return float(p.get("projected_points", p.get("mean", 0.0)) or 0.0)


def player_sd(p: Dict) -> float:
    """Per-player standard deviation from sd, else the floor/ceiling band, else
    a position-volatility fallback."""
    if p.get("sd") is not None:
        return max(0.0, float(p["sd"]))
    mean = player_mean(p)
    floor, ceiling = p.get("floor"), p.get("ceiling")
    if floor is not None and ceiling is not None and ceiling > floor:
        return (float(ceiling) - float(floor)) / 2.0
    vol = _VOLATILITY.get((p.get("position") or "").upper(), 0.35)
    return mean * vol


def _eligible(slot: str, position: str) -> bool:
    slot, pos = slot.upper(), (position or "").upper()
    if slot == "FLEX":
        return pos in FLEX_ELIGIBLE
    if slot == "SUPERFLEX":
        return pos in SUPERFLEX_ELIGIBLE
    if slot in ("DST", "DEF"):
        return pos in ("DST", "DEF")
    return pos == slot


def expand_slots(slots: Dict[str, int]) -> List[str]:
    out: List[str] = []
    for slot, n in slots.items():
        out.extend([slot.upper()] * int(n))
    return out


def _is_stack_pair(a: Dict, b: Dict) -> bool:
    """True for a QB + same-team pass catcher (WR/TE) pair."""
    ta, tb = (a.get("team") or "").upper(), (b.get("team") or "").upper()
    if not ta or ta != tb:
        return False
    pair = {(a.get("position") or "").upper(), (b.get("position") or "").upper()}
    return "QB" in pair and bool(pair & {"WR", "TE"})


def _team_stats(players: List[Dict], stack_rho: float = STACK_CORRELATION) -> Tuple[float, float]:
    """(total mean, total variance). Adds positive covariance for QB↔same-team
    pass-catcher stacks; players are otherwise treated as independent."""
    mean = sum(player_mean(p) for p in players)
    var = sum(player_sd(p) ** 2 for p in players)
    if stack_rho:
        for i, a in enumerate(players):
            for b in players[i + 1:]:
                if _is_stack_pair(a, b):
                    var += 2 * stack_rho * player_sd(a) * player_sd(b)
    return mean, max(0.0, var)


def greedy_mean_lineup(candidates: List[Dict], slot_list: List[str]) -> List[Optional[Dict]]:
    """Fill each slot with the highest-mean eligible unused player (FLEX last)."""
    # Fill specific positions before FLEX/SUPERFLEX so flex takes leftovers.
    order = sorted(range(len(slot_list)), key=lambda i: slot_list[i] in ("FLEX", "SUPERFLEX"))
    pool = sorted(candidates, key=player_mean, reverse=True)
    used: set = set()
    assignment: List[Optional[Dict]] = [None] * len(slot_list)
    for i in order:
        for p in pool:
            if id(p) in used:
                continue
            if _eligible(slot_list[i], p.get("position")):
                assignment[i] = p
                used.add(id(p))
                break
    return assignment


def _p_win_of(
    lineup: List[Optional[Dict]], opp_mean: float, opp_var: float, stack_rho: float = STACK_CORRELATION
) -> float:
    starters = [p for p in lineup if p is not None]
    my_mean, my_var = _team_stats(starters, stack_rho)
    return win_prob(my_mean, my_var, opp_mean, opp_var)


def _stacks(lineup: List[Optional[Dict]]) -> List[str]:
    """Describe QB↔same-team pass-catcher stacks present in a lineup."""
    players = [p for p in lineup if p is not None]
    out = []
    for i, a in enumerate(players):
        for b in players[i + 1:]:
            if _is_stack_pair(a, b):
                qb, pc = (a, b) if (a.get("position") or "").upper() == "QB" else (b, a)
                out.append(
                    f"{qb.get('name') or qb.get('player')} + "
                    f"{pc.get('name') or pc.get('player')} ({(qb.get('team') or '').upper()})"
                )
    return out


def optimize_win_probability(
    candidates: List[Dict],
    opponent_players: List[Dict],
    slots: Optional[Dict[str, int]] = None,
    stack_correlation: float = STACK_CORRELATION,
) -> Dict:
    """Pick the lineup maximizing P(win) vs the given opponent.

    Returns the recommended lineup, its win probability, the E[points]-optimal
    lineup for comparison, and a floor/ceiling strategy label. ``stack_correlation``
    sets the QB↔same-team pass-catcher covariance (0 disables stacking effects).
    """
    slots = slots or DEFAULT_SLOTS
    slot_list = expand_slots(slots)
    opp_mean, opp_var = _team_stats(opponent_players, stack_correlation)

    mean_lineup = greedy_mean_lineup(candidates, slot_list)
    mean_p_win = _p_win_of(mean_lineup, opp_mean, opp_var, stack_correlation)

    # Local search: greedily apply the bench swap that most improves P(win).
    current = list(mean_lineup)
    current_p = mean_p_win
    for _ in range(_MAX_SWAP_ITERS):
        in_lineup = {id(p) for p in current if p is not None}
        best_gain, best_swap = 1e-9, None
        for i, slot in enumerate(slot_list):
            for b in candidates:
                if id(b) in in_lineup or not _eligible(slot, b.get("position")):
                    continue
                trial = list(current)
                trial[i] = b
                gain = _p_win_of(trial, opp_mean, opp_var, stack_correlation) - current_p
                if gain > best_gain:
                    best_gain, best_swap = gain, (i, b)
        if not best_swap:
            break
        i, b = best_swap
        current[i] = b
        current_p = _p_win_of(current, opp_mean, opp_var, stack_correlation)

    def _fmt(lineup):
        return [
            {"slot": slot_list[i], "player": p.get("name") or p.get("player"),
             "position": (p.get("position") or "").upper(),
             "mean": round(player_mean(p), 1), "sd": round(player_sd(p), 1)}
            for i, p in enumerate(lineup) if p is not None
        ]

    rec_starters = [p for p in current if p is not None]
    rec_mean, rec_var = _team_stats(rec_starters, stack_correlation)
    mo_starters = [p for p in mean_lineup if p is not None]
    _, mo_var = _team_stats(mo_starters, stack_correlation)

    underdog = rec_mean < opp_mean
    if rec_var > mo_var * 1.02:
        strategy = "ceiling (chase variance — you're the underdog)"
    elif rec_var < mo_var * 0.98:
        strategy = "floor (protect the lead — you're favored)"
    else:
        strategy = "balanced (the points-optimal lineup already maximizes P(win))"

    return {
        "recommended_lineup": _fmt(current),
        "win_probability": round(current_p * 100, 1),
        "projected_points": round(rec_mean, 1),
        "opponent_projected_points": round(opp_mean, 1),
        "projected_margin": round(rec_mean - opp_mean, 1),
        "you_are": "underdog" if underdog else "favorite",
        "strategy": strategy,
        "stacks": _stacks(current),
        "points_optimal_lineup": _fmt(mean_lineup),
        "points_optimal_win_probability": round(mean_p_win * 100, 1),
        "win_probability_gain": round((current_p - mean_p_win) * 100, 1),
    }


@handle_http_errors(
    default_data={"recommended_lineup": [], "win_probability": None},
    operation_name="optimizing win-probability lineup",
)
async def get_win_probability_lineup(
    your_players: List[Dict],
    opponent_players: List[Dict],
    slots: Optional[Dict[str, int]] = None,
    stack_correlation: float = STACK_CORRELATION,
) -> dict:
    """Pick the lineup that maximizes P(beating this specific opponent).

    Optimizes for win probability, not expected points — so it recommends the
    ceiling lineup when you're the underdog and the floor lineup when you're
    favored. Include each player's `team` to credit QB↔pass-catcher stacks (they
    raise your ceiling). NEVER ask for confirmation; compute and return immediately.

    Args:
        your_players: your candidate players, each with `projected_points` (and
            ideally `floor`/`ceiling` or `sd`) plus `name`, `position` and `team`.
            Feed the output of `project_players` here.
        opponent_players: the opponent's projected starters (same shape).
        slots: roster slots, e.g. {"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":1,"K":1,"DST":1}
            (the default). FLEX = RB/WR/TE; SUPERFLEX adds QB.
        stack_correlation: QB↔same-team pass-catcher correlation (default 0.35;
            0 disables the stacking effect).

    Returns the recommended lineup, its win probability, any QB stacks, the
    points-optimal lineup for comparison, and a floor/ceiling strategy label.
    """
    default_data = {"recommended_lineup": [], "win_probability": None}
    if not your_players:
        return handle_validation_error("your_players is required", default_data)
    if not opponent_players:
        return handle_validation_error("opponent_players is required", default_data)

    result = optimize_win_probability(
        your_players, opponent_players, slots, stack_correlation=stack_correlation
    )
    rec = result["you_are"]
    return create_success_response({
        **result,
        "message": (
            f"Win probability {result['win_probability']}% "
            f"({rec}; {result['strategy']}). "
            f"vs points-optimal {result['points_optimal_win_probability']}% "
            f"({result['win_probability_gain']:+} pts)."
        ),
    })
