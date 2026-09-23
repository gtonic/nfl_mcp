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
from datetime import UTC, datetime

from .errors import create_success_response, handle_http_errors, handle_validation_error
from .game_clock import game_lock, week_games
from .lineup_slots import (
    SLOT_ELIGIBILITY,
    assign_to_slots,
    normalize_slot,
    optimal_lineup,
    slot_accepts,
)
from .lineup_slots import expand_slots as expand_slots
from .projections import _VOLATILITY
from .teams import normalize_team

FLEX_ELIGIBLE = set(SLOT_ELIGIBILITY["FLEX"])
SUPERFLEX_ELIGIBLE = set(SLOT_ELIGIBILITY["SUPERFLEX"])
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


def player_mean(p: dict) -> float:
    return float(p.get("projected_points", p.get("mean", 0.0)) or 0.0)


def player_sd(p: dict) -> float:
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
    """Kept for callers; the rules live in `lineup_slots`."""
    return slot_accepts(slot, position)


def _is_stack_pair(a: dict, b: dict) -> bool:
    """True for a QB + same-team pass catcher (WR/TE) pair."""
    ta, tb = (a.get("team") or "").upper(), (b.get("team") or "").upper()
    if not ta or ta != tb:
        return False
    pair = {(a.get("position") or "").upper(), (b.get("position") or "").upper()}
    return "QB" in pair and bool(pair & {"WR", "TE"})


def _team_stats(players: list[dict], stack_rho: float = STACK_CORRELATION) -> tuple[float, float]:
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


def mean_optimal_lineup(candidates: list[dict], slot_list: list[str]) -> list[dict | None]:
    """The E[points]-optimal legal lineup, one entry per slot (``None`` if empty).

    An exact assignment rather than a greedy fill: filling a SUPERFLEX before a
    FLEX used to hand the superflex the best leftover RB and leave the FLEX
    empty with a QB on the bench.
    """
    return optimal_lineup(candidates, slot_list, value=player_mean)


# Old name, kept so external callers keep working.
greedy_mean_lineup = mean_optimal_lineup


def _p_win_of(
    lineup: list[dict | None], opp_mean: float, opp_var: float, stack_rho: float = STACK_CORRELATION
) -> float:
    starters = [p for p in lineup if p is not None]
    my_mean, my_var = _team_stats(starters, stack_rho)
    return win_prob(my_mean, my_var, opp_mean, opp_var)


def _stacks(lineup: list[dict | None]) -> list[str]:
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
    candidates: list[dict],
    opponent_players: list[dict],
    slots: dict[str, int] | None = None,
    stack_correlation: float = STACK_CORRELATION,
    locked_players: list[dict] | None = None,
) -> dict:
    """Pick the lineup maximizing P(win) vs the given opponent.

    ``locked_players`` are already committed and cannot be changed — mid-week,
    anyone whose game has kicked off. Each entry should carry its slot under
    ``"slot"``. They contribute to the team total and their slots are removed
    from the optimization, so the search only considers what you can still
    move. A settled player carries ``sd`` 0, which correctly makes the outcome
    less uncertain rather than merely shifting the mean.

    Returns the recommended lineup, its win probability, the E[points]-optimal
    lineup for comparison, and a floor/ceiling strategy label. ``stack_correlation``
    sets the QB↔same-team pass-catcher covariance (0 disables stacking effects).
    """
    slots = slots or DEFAULT_SLOTS
    slot_list = expand_slots(slots)
    locked_players = locked_players or []

    # Remove one slot per locked player, matching on the slot they occupy.
    if locked_players:
        remaining = list(slot_list)
        for p in locked_players:
            slot = normalize_slot(p.get("slot"))
            if slot in remaining:
                remaining.remove(slot)
            elif remaining:
                # Slot missing or renamed: drop the narrowest slot the player
                # is eligible for (his own position before any flex) rather
                # than optimizing a seat that is taken. No eligible slot (an
                # IDP starter in a list without IDP slots): remove nothing —
                # taking remaining[0] silently deleted the QB seat.
                eligible = [s for s in remaining if slot_accepts(s, p.get("position"))]
                if eligible:
                    remaining.remove(min(eligible, key=lambda s: len(SLOT_ELIGIBILITY.get(s, ()))))
        slot_list = remaining

    locked_mean, locked_var = _team_stats(locked_players, stack_correlation)
    opp_mean, opp_var = _team_stats(opponent_players, stack_correlation)

    # Optimize the open slots against a residual target: locked points are a
    # certainty on my side, so P(locked + open > opp) == P(open > opp - locked).
    residual_mean = opp_mean - locked_mean
    residual_var = opp_var + locked_var

    mean_lineup = mean_optimal_lineup(candidates, slot_list)
    mean_p_win = _p_win_of(mean_lineup, residual_mean, residual_var, stack_correlation)

    # Local search over *who* starts: bring a bench player in for any starter
    # (or into an empty slot) whenever some legal arrangement of the new set
    # exists — which may move other starters between slots — and keep the
    # change that most improves P(win). P(win) depends only on the set.
    current = list(mean_lineup)
    current_p = mean_p_win
    for _ in range(_MAX_SWAP_ITERS):
        in_lineup = {id(p) for p in current if p is not None}
        bench = [b for b in candidates if id(b) not in in_lineup]
        best_gain, best_trial = 1e-9, None
        for i in range(len(current)):
            for b in bench:
                if slot_accepts(slot_list[i], b.get("position")):
                    trial = list(current)
                    trial[i] = b
                else:
                    starters = [p for j, p in enumerate(current) if p is not None and j != i]
                    trial = assign_to_slots([*starters, b], slot_list)
                    if trial is None:
                        continue
                gain = _p_win_of(trial, residual_mean, residual_var, stack_correlation) - current_p
                if gain > best_gain:
                    best_gain, best_trial = gain, trial
        if best_trial is None:
            break
        current = best_trial
        current_p = _p_win_of(current, residual_mean, residual_var, stack_correlation)

    def _fmt(lineup):
        return [
            {"slot": slot_list[i], "player": p.get("name") or p.get("player"),
             "position": (p.get("position") or "").upper(),
             "mean": round(player_mean(p), 1), "sd": round(player_sd(p), 1)}
            for i, p in enumerate(lineup) if p is not None
        ]

    rec_starters = [p for p in current if p is not None]
    rec_mean, rec_var = _team_stats(rec_starters, stack_correlation)
    rec_mean += locked_mean
    rec_var += locked_var
    mo_starters = [p for p in mean_lineup if p is not None]
    _, mo_var = _team_stats(mo_starters, stack_correlation)
    mo_var += locked_var

    underdog = rec_mean < opp_mean
    if rec_var > mo_var * 1.02:
        strategy = "ceiling (chase variance — you're the underdog)"
    elif rec_var < mo_var * 0.98:
        strategy = "floor (protect the lead — you're favored)"
    else:
        strategy = "balanced (the points-optimal lineup already maximizes P(win))"

    locked_fmt = [
        {"slot": (p.get("slot") or "").upper(), "player": p.get("name") or p.get("player"),
         "position": (p.get("position") or "").upper(),
         "mean": round(player_mean(p), 1), "sd": round(player_sd(p), 1), "locked": True}
        for p in locked_players
    ]

    return {
        "recommended_lineup": locked_fmt + _fmt(current),
        "locked_players": locked_fmt,
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


_BENCH_SLOTS = {"BN", "BENCH", "IR", "TAXI", ""}


def _now() -> datetime:
    """The clock kickoff locks are read against; a seam for tests."""
    return datetime.now(UTC)


async def _split_by_kickoff(
    players: list[dict], season: int | None, week: int | None, db=None
) -> tuple[list[dict], list[dict], list[dict], dict[str, dict], int | None, int | None]:
    """``(movable, locked, unavailable, lock_by_name, season, week)``.

    A player whose game has started is either locked in the slot he holds
    (his ``slot`` names a starting seat) or, benched or with no slot given,
    unavailable: he can no longer be moved in. Everyone else stays a
    candidate. Only players carrying a ``team`` can be checked.
    """
    from .database import get_shared_db
    from .week_context import resolve_season_week

    season, week, _ = await resolve_season_week(season, week)
    try:
        games = week_games(db or get_shared_db(), season, week)
    except Exception:
        games = {}
    now = _now()
    movable, locked, unavailable = [], [], []
    lock_by_name: dict[str, dict] = {}
    for p in players:
        lock = game_lock(games.get(normalize_team(p.get("team")) or ""), now)
        lock_by_name[p.get("name") or p.get("player") or ""] = lock
        if not lock["locked"]:
            movable.append(p)
        elif (p.get("slot") or "").upper() not in _BENCH_SLOTS:
            locked.append(p)
        else:
            unavailable.append(p)
    return movable, locked, unavailable, lock_by_name, season, week


def _with_kickoffs(entries: list[dict], lock_by_name: dict[str, dict]) -> list[dict]:
    """Lineup entries with their player's kickoff and lock fields."""
    out = []
    for entry in entries:
        lock = lock_by_name.get(entry.get("player") or "")
        if lock:
            entry = {**entry, "kickoff": lock["kickoff"], "kickoff_local": lock["kickoff_local"],
                     "kickoff_weekday": lock["kickoff_weekday"], "locked": lock["locked"]}
        out.append(entry)
    return out


@handle_http_errors(
    default_data={"recommended_lineup": [], "win_probability": None},
    operation_name="optimizing win-probability lineup",
)
async def get_win_probability_lineup(
    your_players: list[dict],
    opponent_players: list[dict],
    slots: dict[str, int] | None = None,
    stack_correlation: float = STACK_CORRELATION,
    locked_players: list[dict] | None = None,
    season: int | None = None,
    week: int | None = None,
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
            (the default). FLEX = RB/WR/TE, WRRB_FLEX = RB/WR, REC_FLEX = WR/TE,
            SUPERFLEX (or SUPER_FLEX) adds QB; DEF and DST are the same slot.
        stack_correlation: QB↔same-team pass-catcher correlation (default 0.35;
            0 disables the stacking effect).
        locked_players: players already committed — mid-week, anyone whose game
            has kicked off. Each needs its `slot`; they count toward the total
            and their slots leave the optimization. Omit it to have kickoffs
            read from the cached schedule: a player (with `team`) whose game
            has started is locked in his current `slot`, or — benched or with
            no slot given — left out, since he can no longer be moved in.
        season, week: the week those kickoffs are read for (default: current).

    Returns the recommended lineup, its win probability, any QB stacks, the
    points-optimal lineup for comparison, and a floor/ceiling strategy label.
    """
    default_data = {"recommended_lineup": [], "win_probability": None}
    if not your_players:
        return handle_validation_error("your_players is required", default_data)
    if not opponent_players:
        return handle_validation_error("opponent_players is required", default_data)

    unavailable: list[dict] = []
    lock_by_name: dict[str, dict] = {}
    if locked_players is None and any(p.get("team") for p in your_players):
        your_players, locked_players, unavailable, lock_by_name, season, week = (
            await _split_by_kickoff(your_players, season, week))
        if not your_players and not locked_players:
            return handle_validation_error(
                "every one of your_players has already kicked off from the bench — "
                "nothing left to optimize", default_data)

    result = optimize_win_probability(
        your_players, opponent_players, slots,
        stack_correlation=stack_correlation, locked_players=locked_players,
    )
    if lock_by_name:
        for key in ("recommended_lineup", "locked_players", "points_optimal_lineup"):
            result[key] = _with_kickoffs(result[key], lock_by_name)
    rec = result["you_are"]
    return create_success_response({
        **result,
        # Benched (or slot-less) players whose game has started: out of reach.
        "unavailable_started": [
            {"player": p.get("name") or p.get("player"),
             "kickoff_local": (lock_by_name.get(p.get("name") or p.get("player") or "")
                               or {}).get("kickoff_local")}
            for p in unavailable
        ],
        "kickoffs_checked": bool(lock_by_name),
        "season": season,
        "week": week,
        "message": (
            f"Win probability {result['win_probability']}% "
            f"({rec}; {result['strategy']}). "
            f"vs points-optimal {result['points_optimal_win_probability']}% "
            f"({result['win_probability_gain']:+} pts)."
        ),
    })
