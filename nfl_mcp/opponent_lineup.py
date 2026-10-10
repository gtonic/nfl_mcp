"""The opponent's week, two ways: the lineup he has SET, and his BEST one.

Every win probability in the server (the briefing, ``risk_mode`` auto, the
lineup grade's risk block, ``compare_players_for_slot``'s matchup context)
projects the opponent's *set* starters — Sleeper's matchup ``starters`` —
because that is the lineup that scores if he does nothing. It is the honest
number, but it moves when he fixes his lineup: an empty slot, a starter on
bye, or an Out player left in the lineup projects at zero, and the day he
swaps them out the opponent's total jumps (VLBG week 5 2026: 53.7 → 80.0
overnight, three Out starters replaced). So both are reported:

- ``set_lineup_points`` — what P(win) is computed against (the basis);
- ``best_lineup_points`` — his best legal lineup from the players he could
  still start (open set starters + bench players whose game has not kicked
  off; locked starters stay in their slot), i.e. the "if he fixes his
  lineup" risk figure, with the P(win) against it;
- ``lineup_issues`` — the set starters that cost him points: empty slot,
  bye, Out / inactive, Doubtful, unprojectable, projected zero.

A starter whose game has kicked off counts with his actual points (settled
for a game in progress), the opponent's as well as yours.
"""
from __future__ import annotations

import math

from .game_clock import progress_of, settle
from .lineup_slots import optimal_lineup
from .teams import normalize_team

# Issues in the order they are worth reading.
ISSUE_ORDER = ("empty", "bye", "out", "inactive", "doubtful", "unprojected", "zero_projection")


def _mean(p: dict) -> float:
    return float(p.get("projected_points", p.get("mean", 0.0)) or 0.0)


def _issue_of(cand: dict) -> str | None:
    from .injury_status import availability
    if cand.get("gameday_status") == "inactive":
        return "inactive"
    kind = availability(cand.get("injury_status"))
    if kind == "out":
        return "out"
    if kind == "doubtful":
        return "doubtful"
    if _mean(cand) <= 0.0:
        return "zero_projection"
    return None


def assess_opponent_lineup(
    slot_names: list[str],
    starters_raw: list[str],
    candidates: dict[str, dict],
    bench_ids: list[str] | None,
    labels: dict[str, dict] | None,
    games: dict[str, dict],
    points: dict[str, float] | None,
) -> dict:
    """Set vs best lineup for one opponent (see the module doc); pure.

    ``slot_names``: the league's starting slots in Sleeper's order;
    ``starters_raw``: his matchup ``starters`` in that order ("0" = empty);
    ``candidates``: ``{player_id: projection candidate}`` (name, position,
    team, projected_points, floor, ceiling, injury_status?, gameday_status?)
    for every player that could be projected, starters and bench;
    ``bench_ids``: his startable non-starters (no reserve / taxi), None when
    unknown (then best = set); ``labels``: ``{player_id: {name, position,
    team, reason}}`` for players that could not be projected (reason "bye" /
    "unprojectable"); ``games``: `game_clock.week_games`; ``points``: the
    matchup's ``players_points``.
    """
    from .win_probability import _team_stats

    points = points or {}
    labels = labels or {}
    set_rows: list[dict] = []
    set_players: list[dict] = []
    fixed: list[tuple[int, dict]] = []      # (slot index, locked player)
    open_starters: list[dict] = []
    issues: list[dict] = []
    for i, slot in enumerate(slot_names):
        pid = str(starters_raw[i]) if i < len(starters_raw) and starters_raw[i] else "0"
        if pid in ("0", "", "None"):
            issues.append({"slot": slot, "player": None, "issue": "empty",
                           "projected_points": 0.0})
            set_rows.append({"slot": slot, "player": None, "projected_points": 0.0,
                             "issue": "empty"})
            continue
        cand = candidates.get(pid)
        label = labels.get(pid) or {}
        team = normalize_team((cand or label).get("team")) or ""
        progress = progress_of(games.get(team))
        if cand is None:
            # Not projectable (bye, no team, cold schedule). Locked with his
            # real points if his game has started; otherwise a zero.
            name = label.get("name") or pid
            if progress > 0.0 and pid in points:
                actual = float(points.get(pid) or 0.0)
                entry = {"name": name, "player_id": pid, "position": label.get("position"),
                         "team": team, "projected_points": round(actual, 2), "sd": 0.0,
                         "slot": slot, "locked": True, "actual": actual}
                fixed.append((i, entry))
                set_players.append(entry)
                set_rows.append({"slot": slot, "player": name, "projected_points": actual,
                                 "locked": True, "actual": actual})
                continue
            reason = "bye" if label.get("reason") == "bye" else "unprojected"
            issues.append({"slot": slot, "player": name, "issue": reason,
                           "projected_points": 0.0})
            set_rows.append({"slot": slot, "player": name, "projected_points": 0.0,
                             "issue": reason})
            continue
        if progress > 0.0:
            mean, share = settle(_mean(cand), points.get(pid), progress)
            sd = (float(cand.get("ceiling") or 0) - float(cand.get("floor") or 0)) / 2.0 * share
            entry = {**cand, "projected_points": round(mean, 2), "sd": round(sd, 2),
                     "slot": slot, "locked": True, "actual": points.get(pid)}
            fixed.append((i, entry))
            set_players.append(entry)
            set_rows.append({"slot": slot, "player": cand.get("name"),
                             "projected_points": round(mean, 2), "locked": True,
                             "actual": points.get(pid), "game_progress": round(progress, 2)})
            continue
        issue = _issue_of(cand)
        set_players.append(cand)
        open_starters.append(cand)
        row = {"slot": slot, "player": cand.get("name"),
               "projected_points": round(_mean(cand), 2)}
        if issue:
            row["issue"] = issue
            issues.append({"slot": slot, "player": cand.get("name"), "issue": issue,
                           "projected_points": round(_mean(cand), 2),
                           "injury_status": cand.get("injury_status")})
        set_rows.append(row)

    set_mean, set_var = _team_stats(set_players)
    if bench_ids is None:
        best_players, start, sit = list(set_players), [], []
    else:
        bench = []
        for pid in bench_ids:
            cand = candidates.get(str(pid))
            if not cand:
                continue
            if progress_of(games.get(normalize_team(cand.get("team")) or "")) > 0.0:
                continue  # kicked off on his bench: can no longer be moved in
            bench.append(cand)
        locked_idx = {i for i, _ in fixed}
        open_slots = [s for i, s in enumerate(slot_names) if i not in locked_idx]
        fill = optimal_lineup(open_starters + bench, open_slots, value=_mean)
        best_players = [p for _, p in fixed] + [p for p in fill if p is not None]
        best_ids = {id(p) for p in best_players}
        start = [{"player": p.get("name"), "position": p.get("position"),
                  "projected_points": round(_mean(p), 2)}
                 for p in fill if p is not None and all(p is not s for s in open_starters)]
        sit = [{"player": p.get("name"), "projected_points": round(_mean(p), 2)}
               for p in open_starters if id(p) not in best_ids]
        sit += [{"player": i["player"], "projected_points": 0.0, "issue": i["issue"]}
                for i in issues if i["issue"] in ("bye", "unprojected") and i["player"]]
    best_mean, best_var = _team_stats(best_players)
    if best_mean < set_mean:  # cannot happen with an exact fill; guard rounding
        best_players, best_mean, best_var = list(set_players), set_mean, set_var
    issues.sort(key=lambda x: ISSUE_ORDER.index(x["issue"]))
    return {
        "basis": "set_lineup",
        "set_lineup_points": round(set_mean, 1),
        "set_lineup_sd": round(math.sqrt(max(0.0, set_var)), 1),
        "best_lineup_points": round(best_mean, 1),
        "best_lineup_sd": round(math.sqrt(max(0.0, best_var)), 1),
        "points_at_risk": round(best_mean - set_mean, 1),
        "bench_considered": bench_ids is not None,
        "lineup_issues": issues,
        "set_lineup": set_rows,
        "best_lineup_changes": {"start": start, "bench": sit},
        "locked": [{"player": e.get("name"), "slot": e.get("slot"), "actual": e.get("actual"),
                    "counted": e.get("projected_points")} for _, e in fixed],
        "set_players": set_players,
        "best_players": best_players,
    }


def merge_locked_starters(
    starters: list[str], matchup_starters: list[str] | None,
    team_of, games: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """``(starters, ids taken from the matchup)``: each slot where Sleeper's
    matchup copy holds a player whose game has kicked off keeps him.

    `sleeper_tools.set_starters` falls back to the roster's starters when the
    matchup lists a player no longer on the roster (a stale copy after a
    trade). But a locked starter stays in the matchup -- and his points count
    -- even after he is dropped: VLBG week 5 2026, Jake Ferguson scored 3.4 on
    Thursday and was cut on Friday; the roster showed "0" at TE and the
    briefing called the slot empty and lost his points. ``team_of(pid)`` is
    the player's team (None when unknown).
    """
    out = [str(p) for p in starters]
    taken = []
    for i, pid in enumerate(matchup_starters or []):
        pid = str(pid or "0")
        if pid in ("0", "") or (i < len(out) and out[i] == pid):
            continue
        team = normalize_team(team_of(pid) or "") or ""
        if progress_of(games.get(team)) <= 0.0:
            continue
        while len(out) <= i:
            out.append("0")
        out[i] = pid
        taken.append(pid)
    return out, taken


def explain(assessment: dict) -> str:
    """One sentence on what the opponent number is and what could move it."""
    a = assessment
    head = (f"Opponent projected on the lineup he has set: {a['set_lineup_points']}"
            f" (his best available lineup: {a['best_lineup_points']}).")
    if not a["lineup_issues"]:
        return head + " No empty, bye or injured starters in his lineup."
    named = ", ".join(f"{i['player'] or 'empty'} ({i['slot']}: {i['issue']})"
                      for i in a["lineup_issues"][:5])
    return (head + f" His lineup carries {named}; if he fixes it his total rises by up to "
            f"{a['points_at_risk']} — read win_probability_if_opponent_fixes_lineup too.")


def issues_from_players(players: list[dict] | None) -> list[dict]:
    """Lineup issues of a caller-given opponent list (no slots known):
    players marked on bye, Out / inactive / Doubtful, or projected zero."""
    out = []
    for p in players or []:
        name = p.get("name") or p.get("player")
        if p.get("on_bye") or str(p.get("opponent") or "").upper() == "BYE":
            out.append({"player": name, "issue": "bye", "projected_points": _mean(p)})
            continue
        issue = _issue_of(p)
        if issue:
            out.append({"player": name, "issue": issue, "projected_points": _mean(p),
                        "injury_status": p.get("injury_status")})
    out.sort(key=lambda x: ISSUE_ORDER.index(x["issue"]))
    return out


__all__ = ["assess_opponent_lineup", "explain", "issues_from_players"]
