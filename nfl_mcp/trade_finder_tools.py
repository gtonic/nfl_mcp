"""Which trades are worth proposing, and to whom.

`analyze_trade` grades a trade you already have in mind. The harder half of the
question came first and had no tool: *which* trade. Answering it by hand means
reading eleven rosters and guessing who is thin where.

The measurement is the one thing that makes a trade real — both sides have to
come out ahead, or it never gets accepted. So each candidate swap is scored by
recomputing **both** teams' best legal starting lineups before and after it,
using the same optimizer the weekly briefing uses. A proposal survives only if
both totals go up.

The horizon is the rest of the season by default (``horizon="ros"``): each
roster's lineup is re-optimised for every remaining week on that week's
rest-of-season projection (``ros.py``) and the weeks are summed, so a player on
bye or out for a game is valued for the weeks he does play and the depth that
covers him counts. ``horizon="week"`` keeps the one-week search. The league's
``trade_deadline`` is read first: past it there is nothing to propose.

One-for-one swaps first; with ``max_package_size`` >= 2 (the default) also
packages — 2-for-1, 1-for-2 and 2-for-2, and 3-for-2 / 2-for-3 at size 3.
Packages explode the search space, so they are pruned hard (each side's
surplus plus at most one starter, a received package must hold an upgrade,
padding is dropped, the screen is capped; see `_search_packages`), and a side
receiving more players than it sends drops its least valuable player(s) to
stay roster-legal.

Proposals are ranked by your lineup gain plus a small bonus for timing — a
player sold while his value is high (``sell_high``) or bought while it is low
(``buy_low``), from `value_trajectory` — shown apart from the gain. The
both-sides-gain bar is on lineup points alone.
"""
from __future__ import annotations

import logging

from .briefing_tools import _staleness_warnings
from .database import get_shared_db
from .errors import create_success_response
from .injury_match import build_injury_index, injury_for_row, misses_this_week
from .roster_needs import lineup_slots, replacement_levels, slot_counts, starting_lineup_total
from .teams import normalize_team

logger = logging.getLogger(__name__)

# Below this a "gain" is inside the noise of a weekly projection (MAE ~5.8), so
# presenting it as an upgrade would be false precision.
MEANINGFUL_GAIN = 1.0
# How many players per roster to consider. Beyond the top dozen a roster holds
# only bench filler, which nobody trades for.
CANDIDATES_PER_ROSTER = 12

TRADEABLE_POSITIONS = ("QB", "RB", "WR", "TE")
# The ROS bar: half a point a week over the weeks left, and never less than the
# one-week bar. A 3-point season-long edge is not a reason to trade.
MEANINGFUL_ROS_GAIN_PER_WEEK = 0.5
# Swaps re-scored week by week after the season-total screen; the screen is
# cheap, the weekly re-optimisation is not.
MAX_WEEKLY_RESCORES = 150


def _projection_input(
    row: dict, opponents: dict[str, str], injury_index: dict | None = None
) -> dict | None:
    team = normalize_team(row.get("team_id"))
    position = (row.get("position") or "").upper()
    if not team or position not in TRADEABLE_POSITIONS:
        return None
    name = row.get("full_name")
    if not name:
        return None
    opponent = opponents.get(team)
    if not opponent:
        return None  # bye week, or the schedule cache is cold
    player = {"name": name, "position": position, "team": team,
              "opponent": opponent, "player_id": row.get("id")}
    # Injury-aware, so both lineup totals are the ones that will actually take
    # the field — a partner "gaining" an Out receiver gains nothing this week.
    injury = injury_for_row(row, injury_index or {})
    if injury:
        player["injury"] = {"status": injury["status"]}
    return player


def swap_gain(
    players: list[dict], slots: dict[str, int], give: dict, receive: dict
) -> float:
    """Change in a roster's best starting lineup from a one-for-one swap."""
    before = starting_lineup_total(players, slots)
    after_roster = [p for p in players if p is not give] + [receive]
    return round(starting_lineup_total(after_roster, slots) - before, 2)


def _top_candidates(players: list[dict], limit: int = CANDIDATES_PER_ROSTER) -> list[dict]:
    """The players worth building a swap around, best first.

    Anyone who misses this week is left out on both sides. The search scores a
    trade by this week's lineup, so an Out star would be offered away for
    nothing, and receiving one would look like a loss — both artefacts of the
    one-week horizon, not of his value.
    """
    healthy = [p for p in players if not misses_this_week(p.get("injury_status"))]
    return sorted(
        healthy, key=lambda p: float(p.get("projected_points") or 0.0), reverse=True
    )[:limit]


async def find_trade_targets(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    positions: list[str] | None = None,
    limit: int = 10,
    horizon: str = "ros",
    max_package_size: int = 2,
) -> dict:
    """Find trades (one-for-one, and packages up to `max_package_size` a side)
    that improve both your lineup and theirs."""
    from . import ros, sleeper_tools
    from .briefing_tools import _scoring_label, _scoring_ppr
    from .projections import project_players
    from .scoring import league_scoring

    db = get_shared_db()

    if week is None or season is None:
        state = await sleeper_tools.get_nfl_state()
        nfl_state = (state or {}).get("nfl_state") or {}
        week = week or int(nfl_state.get("week") or 1)
        season = season or int(nfl_state.get("season") or 0)

    league_resp = await sleeper_tools.get_league(league_id)
    league = (league_resp or {}).get("league") or {}
    scoring_exact = league_scoring(league)  # "0.5" + the full scoring_settings
    num_teams = int(league.get("total_rosters") or 12)
    roster_positions = league.get("roster_positions")
    fractional_slots = slot_counts(roster_positions)
    whole_slots = lineup_slots(roster_positions)

    rosters_resp = await sleeper_tools.get_rosters(league_id)
    roster_state = sleeper_tools.roster_freshness(rosters_resp)
    if roster_state["error"]:
        return create_success_response({"success": False, "error": roster_state["error"]})
    rosters = roster_state["rosters"]
    mine, error = sleeper_tools.find_roster(rosters, league_id, roster_id, user_id,
                                            purpose="advise")
    if error:
        return create_success_response({"success": False, "error": error})
    roster_id = mine["roster_id"]

    deadline = ros.trade_deadline_status(league.get("settings") or {}, week)
    if deadline["passed"]:
        return create_success_response({
            "league": {"league_id": league_id, "name": league.get("name")},
            "season": season, "week": week, "roster_id": roster_id,
            "trade_deadline": deadline, "proposals": [], "candidates_considered": 0,
            "message": deadline["message"],
        })

    freshness = db.get_data_freshness()

    opponents: dict[str, str] = {}
    for team, opp in db.get_week_opponents(season, week).items():
        canon_team, canon_opp = normalize_team(team), normalize_team(opp)
        if canon_team and canon_opp:
            opponents[canon_team] = canon_opp
    if not opponents and horizon != "week":
        fetched = (await ros.schedules_for(db, season, [week])).get(week)
        opponents = dict(fetched or {})
    if not opponents:
        return create_success_response({
            "success": False,
            "error": (f"No cached schedule for {season} week {week} — run the "
                      "prefetch (NFL_MCP_PREFETCH=1) or call get_team_schedule first."),
            "season": season, "week": week,
        })

    # Everyone's players in one projection call. Reserve and taxi are excluded:
    # a stashed player cannot hold a slot, so he cannot change a lineup total.
    ids_by_roster: dict[int, list[str]] = {}
    for roster in rosters:
        unavailable = {str(p) for p in (roster.get("reserve") or [])}
        unavailable |= {str(p) for p in (roster.get("taxi") or [])}
        ids_by_roster[roster["roster_id"]] = [
            str(p) for p in (roster.get("players") or []) if str(p) not in unavailable
        ]

    names = await _team_names(sleeper_tools, league_id, rosters)
    if horizon != "week":
        return sleeper_tools.mark_roster_staleness(await _find_ros(
            db=db, league=league, league_id=league_id, rosters=rosters,
            roster_id=roster_id, season=season, week=week, positions=positions,
            limit=limit, names=names, deadline=deadline, freshness=freshness,
            header={
                "league_id": league_id, "name": league.get("name"),
                "scoring": _scoring_label(league), "ppr": _scoring_ppr(league),
                "scoring_used": scoring_exact.model.summary(),
                "num_teams": num_teams,
            },
            whole_slots=whole_slots, fractional_slots=fractional_slots,
            max_package_size=max(1, min(MAX_PACKAGE_SIZE, int(max_package_size or 1))),
        ), roster_state)

    all_ids = [pid for ids in ids_by_roster.values() for pid in ids]
    athlete_rows = db.get_athletes_by_ids(all_ids)
    injury_index = build_injury_index(db.get_all_current_injuries())

    inputs_by_roster: dict[int, list[dict]] = {}
    flat_inputs: list[dict] = []
    for rid, ids in ids_by_roster.items():
        entries = [
            p for pid in ids
            if (row := athlete_rows.get(pid))
            and (p := _projection_input(row, opponents, injury_index))
        ]
        inputs_by_roster[rid] = entries
        flat_inputs.extend(entries)

    projected = await project_players(
        flat_inputs, scoring=scoring_exact, num_teams=num_teams,
        season=season, week=week,
    )
    by_key = {
        (p["player"], p["team"]): p
        for p in (projected or {}).get("projections") or []
    }

    def _scored(entries: list[dict]) -> list[dict]:
        out = []
        for entry in entries:
            projection = by_key.get((entry["name"], entry["team"]))
            if not projection:
                continue
            out.append({
                "player_id": entry["player_id"],
                "injury_status": (entry.get("injury") or {}).get("status"),
                "name": entry["name"],
                "position": entry["position"],
                "team": entry["team"],
                "opponent": entry["opponent"],
                "projected_points": projection["projected_points"],
                "floor": projection["floor"],
                "ceiling": projection["ceiling"],
            })
        return out

    scored_by_roster = {rid: _scored(entries) for rid, entries in inputs_by_roster.items()}
    mine_scored = scored_by_roster.get(roster_id, [])
    if not mine_scored:
        return create_success_response({
            "success": False,
            "error": "Could not project any player on your roster — is the athlete cache warm?",
        })

    wanted = {p.upper() for p in (positions or TRADEABLE_POSITIONS)}
    my_candidates = _top_candidates(mine_scored)

    proposals = []
    evaluated = 0
    skipped_depth = 0
    for other_id, their_scored in scored_by_roster.items():
        if other_id == roster_id or not their_scored:
            continue
        their_candidates = _top_candidates(their_scored)
        for give in my_candidates:
            for receive in their_candidates:
                if receive["position"] not in wanted:
                    continue
                if _over_depth(mine_scored, give, receive, whole_slots):
                    skipped_depth += 1
                    continue
                evaluated += 1
                my_gain = swap_gain(mine_scored, whole_slots, give, receive)
                if my_gain < MEANINGFUL_GAIN:
                    continue
                # The other manager has to win too, or this is a wish, not a
                # trade. Their gain is computed on their roster, their slots.
                their_gain = swap_gain(their_scored, whole_slots, receive, give)
                if their_gain < MEANINGFUL_GAIN:
                    continue
                proposals.append({
                    "partner_roster_id": other_id,
                    "partner": names.get(other_id, f"Roster {other_id}"),
                    "you_give": {k: give[k] for k in
                                 ("player_id", "name", "position", "team", "projected_points")},
                    "you_get": {k: receive[k] for k in
                                ("player_id", "name", "position", "team", "projected_points")},
                    "your_gain": my_gain,
                    "their_gain": their_gain,
                    "mutual_gain": round(my_gain + their_gain, 2),
                })

    # Best for you first; the joint gain breaks ties, because a trade both sides
    # clearly win is the one that actually gets accepted.
    proposals.sort(key=lambda p: (p["your_gain"], p["mutual_gain"]), reverse=True)

    # One proposal per partner keeps the list a set of conversations to have
    # rather than twenty variations on the same one.
    seen_partners: set[int] = set()
    top = []
    for proposal in proposals:
        if proposal["partner_roster_id"] in seen_partners:
            continue
        seen_partners.add(proposal["partner_roster_id"])
        top.append(proposal)
        if len(top) >= limit:
            break

    levels = replacement_levels(mine_scored, fractional_slots, whole_slots)

    return sleeper_tools.mark_roster_staleness(create_success_response({
        "league": {
            "league_id": league_id, "name": league.get("name"),
            "scoring": _scoring_label(league), "ppr": _scoring_ppr(league),
            "scoring_used": scoring_exact.model.summary(),
            "num_teams": num_teams,
        },
        "season": season,
        "week": week,
        "roster_id": roster_id,
        "data_freshness": freshness,
        "stale_data_warnings": _staleness_warnings(freshness),
        "your_replacement_levels": {k: round(v, 1) for k, v in levels.items()},
        "proposals": top,
        # Every swap scored, not only the ones that survived.
        "candidates_considered": evaluated,
        # Every mutual-gain swap vs the ones listed (best one per partner).
        "proposals_found": len(proposals),
        "proposals_listed": len(top),
        "skipped_over_depth": skipped_depth,
        "trade_deadline": deadline,
        "horizon": "week",
        # Kept out of every proposal, yours and theirs; see `_top_candidates`.
        "skipped_injured": sorted(
            f"{p['name']} ({p['injury_status']})"
            for scored in scored_by_roster.values() for p in scored
            if misses_this_week(p.get("injury_status"))
        ),
        "method": (
            "one-for-one swaps scored by recomputing both teams' best legal "
            "starting lineup before and after; only trades where BOTH sides gain "
            f"at least {MEANINGFUL_GAIN} projected points are kept"
        ),
        "caveats": [
            "Gains are for this week's lineup, not rest-of-season value "
            "(horizon='ros' for that) — check the deal with analyze_trade before "
            "sending it.",
            "One-for-one only; a package deal is a different search.",
            "Players who miss this week (Out, Doubtful, IR, …) are not traded "
            "either way — see skipped_injured; value them with analyze_trade.",
        ],
        "message": (
            f"{len(top)} trade(s) listed (best per partner, of {len(proposals)} found) "
            f"that improve both rosters in week {week}."
            if top else
            f"No one-for-one trade improves both your roster and a partner's in "
            f"week {week} ({evaluated} candidate swaps checked)."
        ) + (f" {deadline['message']}" if deadline["urgent"] else ""),
    }), roster_state)


# Bodies beyond the seats a position can take that are still worth holding.
DEPTH_ALLOWANCE = 2


def _over_depth(mine: list[dict], give: dict, receive: dict, whole_slots: dict[str, int]) -> bool:
    """Whether taking ``receive`` for ``give`` piles up a position past need.

    A 1-QB league roster that already holds three QBs has no use for a
    fourth: allowed are the seats that can hold the position (its own and
    any flex that takes it) plus ``DEPTH_ALLOWANCE``.
    """
    from .lineup_slots import slot_accepts
    pos = receive.get("position")
    if not pos or pos == give.get("position"):
        return False
    seats = sum(n for slot, n in whole_slots.items() if slot_accepts(slot, pos))
    held = sum(1 for p in mine if p.get("position") == pos)
    return held + 1 > seats + DEPTH_ALLOWANCE


async def _team_names(sleeper_tools, league_id: str, rosters: list[dict]) -> dict[int, str]:
    """``{roster_id: manager display name}``; cosmetic, so never fatal."""
    names: dict[int, str] = {}
    try:
        users_resp = await sleeper_tools.get_league_users(league_id)
        user_names = {
            u.get("user_id"): u.get("display_name")
            for u in (users_resp or {}).get("users") or []
        }
        for roster in rosters:
            names[roster["roster_id"]] = (
                user_names.get(roster.get("owner_id")) or f"Roster {roster['roster_id']}"
            )
    except Exception as e:  # names are cosmetic; never fail the search on them
        logger.debug(f"league user names unavailable: {e}")
    return names


_ROS_FIELDS = ("player_id", "name", "position", "team", "ros_points", "playoff_points",
               "total_points", "this_week_points", "bye_weeks", "injury_status",
               "injury_weeks")

# --------------------------------------------------------------------------
# Package trades (2-for-1, 2-for-2, 3-for-2)
# --------------------------------------------------------------------------
# Largest package either side may send; `max_package_size` is clamped to it.
MAX_PACKAGE_SIZE = 3
# A player who wins a lineup slot in at least this share of the remaining
# weeks is a starter; below it he is surplus — the pieces a package is built
# from.
STARTER_WEEK_SHARE = 0.5
# Pruning: per roster, the best surplus players and the best starters that
# packages are built from, and how many starters one package may hold. A
# package is the surplus a manager can spare plus at most one starter.
PACKAGE_SURPLUS_PER_ROSTER = 5
PACKAGE_STARTERS_PER_ROSTER = 5
MAX_STARTERS_PER_PACKAGE = 1
# Packages screened on the season-total lineup before the weekly re-score;
# a hard stop on the search whatever the league size.
MAX_PACKAGE_SCREENS = 40000
MAX_PACKAGE_RESCORES = 120
# A package is padding when dropping one of its pieces leaves both sides
# within this many season-total points of the full deal.
PACKAGE_REDUNDANCY_EPS = 0.5
# Ranking bonus per well-timed piece (a sell_high sent, a buy_low received;
# `value_trajectory.timing_score`), per week scored. Reported apart from the
# lineup gain and never used to pass the both-sides-gain bar.
TRAJECTORY_BONUS_PER_WEEK = 0.25
# Roster slots that do not count toward the roster limit.
_NON_ROSTER_SLOTS = {"IR", "TAXI"}


def package_shapes(max_size: int) -> list[tuple[int, int]]:
    """``(you_give, you_get)`` counts searched beyond one-for-one: both sides
    at most `max_size`, at most one player apart, five pieces at most."""
    size = max(1, min(MAX_PACKAGE_SIZE, int(max_size or 1)))
    return [(g, r) for g in range(1, size + 1) for r in range(1, size + 1)
            if (g, r) != (1, 1) and abs(g - r) <= 1 and g + r <= 5]


def week_starts(players: list[dict], slots: dict[str, int], weeks: list[int]) -> dict[str, int]:
    """``{player_id: weeks he wins a lineup slot}`` over `weeks`."""
    from .roster_needs import starting_lineup
    counts = {str(p.get("player_id")): 0 for p in players}
    for w in weeks:
        week_players = [{**p, "projected_points": (p.get("weekly_points") or {}).get(w, 0.0)}
                        for p in players]
        for p in starting_lineup(week_players, slots):
            if (p.get("projected_points") or 0) > 0:
                counts[str(p.get("player_id"))] += 1
    return counts


def package_pool(players: list[dict], starts: dict[str, int],
                 n_weeks: int) -> tuple[list[dict], list[dict]]:
    """``(surplus, starters)``: the pieces packages are built from, best first."""
    bar = STARTER_WEEK_SHARE * max(1, n_weeks)
    ranked = sorted((p for p in players if float(p.get("total_points") or 0) > 0),
                    key=lambda p: p["total_points"], reverse=True)
    surplus = [p for p in ranked if starts.get(str(p.get("player_id")), 0) < bar]
    starters = [p for p in ranked if starts.get(str(p.get("player_id")), 0) >= bar]
    return surplus[:PACKAGE_SURPLUS_PER_ROSTER], starters[:PACKAGE_STARTERS_PER_ROSTER]


def packages(surplus: list[dict], starters: list[dict], size: int) -> list[tuple[dict, ...]]:
    """Every `size`-player package of surplus with at most
    `MAX_STARTERS_PER_PACKAGE` starters."""
    from itertools import combinations
    starter_ids = {id(p) for p in starters}
    out = []
    for combo in combinations([*surplus, *starters], size):
        if sum(1 for p in combo if id(p) in starter_ids) <= MAX_STARTERS_PER_PACKAGE:
            out.append(combo)
    return out


def roster_after(players: list[dict], give: tuple[dict, ...], get: tuple[dict, ...],
                 free_slots: int) -> tuple[list[dict], list[dict]]:
    """``(roster after the trade, players it has to drop)``.

    Receiving more players than it sends, a full roster cuts its least
    valuable active players (never one it just received, never one on IR —
    reserve slots do not count toward the limit) to stay legal.
    """
    give_ids = {id(p) for p in give}
    kept = [p for p in players if id(p) not in give_ids]
    need = len(get) - len(give) - max(0, free_slots)
    drops: list[dict] = []
    if need > 0:
        droppable = sorted((p for p in kept if not p.get("on_reserve")),
                           key=lambda p: float(p.get("total_points") or 0.0))
        drops = droppable[:need]
        drop_ids = {id(p) for p in drops}
        kept = [p for p in kept if id(p) not in drop_ids]
    return kept + list(get), drops


def _free_slots(roster: dict, roster_positions: list[str] | None) -> int:
    """Open roster spots: the league's non-IR/taxi slots minus the players
    holding one (K and DEF included)."""
    capacity = sum(1 for s in roster_positions or [] if str(s).upper() not in _NON_ROSTER_SLOTS)
    if not capacity:
        return 0
    held = {str(p) for p in (roster.get("players") or [])}
    held -= {str(p) for p in (roster.get("reserve") or [])}
    held -= {str(p) for p in (roster.get("taxi") or [])}
    return max(0, capacity - len(held))


def _pile_up(after: list[dict], before: list[dict], whole_slots: dict[str, int]) -> bool:
    """Whether a trade leaves a position it added to past `_over_depth`'s limit."""
    from .lineup_slots import slot_accepts
    for pos in {p.get("position") for p in after}:
        held = sum(1 for p in after if p.get("position") == pos)
        if held <= sum(1 for p in before if p.get("position") == pos):
            continue
        seats = sum(n for slot, n in whole_slots.items() if slot_accepts(slot, pos))
        if held > seats + DEPTH_ALLOWANCE:
            return True
    return False


def _player_out(p: dict) -> dict:
    from .value_trajectory import compact
    out = {k: p.get(k) for k in _ROS_FIELDS}
    out["value_trajectory"] = compact(p.get("value_trajectory"))
    return out


def _timing(gives: list[dict], gets: list[dict], n_weeks: int) -> dict:
    """The trade's sell-high / buy-low read and its ranking bonus."""
    from .value_trajectory import side_notes, timing_score
    score = timing_score(gives, gets)
    named = [{**p, "name": p.get("name") or p.get("player")} for p in gives], \
        [{**p, "name": p.get("name") or p.get("player")} for p in gets]
    return {"score": score,
            "bonus": round(score * TRAJECTORY_BONUS_PER_WEEK * max(1, n_weeks), 1),
            "notes": side_notes(*named)}


async def _find_ros(
    *, db, league: dict, league_id: str, rosters: list[dict], roster_id: int,
    season: int, week: int, positions: list[str] | None, limit: int,
    names: dict[int, str], deadline: dict, freshness: dict, header: dict,
    whole_slots: dict[str, int], fractional_slots: dict[str, float],
    max_package_size: int = 2,
) -> dict:
    """The rest-of-season search: every week's best lineup, before and after.

    Two passes, because re-optimising ~15 weekly lineups for every one of
    ~1,500 swaps is too slow to run per request. A season-total lineup
    (each player at his summed ROS points) screens the swaps; the ones that
    help both sides on that screen are re-scored week by week, which is where
    a bye or an injury absence covered by depth shows up.

    With ``max_package_size`` >= 2 packages are searched the same way (see
    `_search_packages`).
    """
    import time

    from . import ros
    from .value_trajectory import annotate

    started = time.monotonic()
    # Reserve players are tradeable and counted: their ROS already carries the
    # expected absence, and the weeks after it are exactly what a trade buys.
    ids_by_roster: dict[int, list[str]] = {}
    reserve_ids: set[str] = set()
    for roster in rosters:
        taxi = {str(p) for p in (roster.get("taxi") or [])}
        reserve_ids |= {str(p) for p in (roster.get("reserve") or [])}
        ids_by_roster[roster["roster_id"]] = [
            str(p) for p in (roster.get("players") or []) if str(p) not in taxi
        ]
    all_ids = [pid for ids in ids_by_roster.values() for pid in ids]
    by_id, meta = await ros.ros_for_ids(all_ids, league=league, season=season,
                                        week=week, db=db)
    weeks = sorted(set(meta["windows"]["regular"]) | set(meta["windows"]["playoff"]))
    # Where each player's trade value is headed, ranked against every
    # rostered player in the league.
    annotate(list(by_id.values()), week=week)
    projected_at = time.monotonic()

    def _scored(ids: list[str]) -> list[dict]:
        out = []
        for pid in ids:
            e = by_id.get(pid)
            if not e or e["position"] not in TRADEABLE_POSITIONS:
                continue
            out.append({**e, "name": e["player"], "on_reserve": pid in reserve_ids,
                        # The screen's lineup value is the summed ROS points.
                        "projected_points": e["total_points"]})
        return out

    scored_by_roster = {rid: _scored(ids) for rid, ids in ids_by_roster.items()}
    mine_scored = scored_by_roster.get(roster_id, [])
    if not mine_scored:
        return create_success_response({
            "success": False,
            "error": "Could not project any player on your roster — is the athlete cache warm?",
        })

    def _top(players: list[dict]) -> list[dict]:
        return sorted(players, key=lambda p: p["total_points"], reverse=True)[
            :CANDIDATES_PER_ROSTER]

    wanted = {p.upper() for p in (positions or TRADEABLE_POSITIONS)}
    bar = max(MEANINGFUL_GAIN, MEANINGFUL_ROS_GAIN_PER_WEEK * len(weeks))
    my_candidates = _top(mine_scored)

    screened = []
    evaluated = 0
    skipped_depth = 0
    for other_id, their_scored in scored_by_roster.items():
        if other_id == roster_id or not their_scored:
            continue
        for give in my_candidates:
            for receive in _top(their_scored):
                if receive["position"] not in wanted:
                    continue
                if _over_depth(mine_scored, give, receive, whole_slots):
                    skipped_depth += 1
                    continue
                evaluated += 1
                my_screen = swap_gain(mine_scored, whole_slots, give, receive)
                if my_screen <= 0:
                    continue
                their_screen = swap_gain(their_scored, whole_slots, receive, give)
                if their_screen <= 0:
                    continue
                screened.append((my_screen + their_screen, other_id, give, receive))

    screened.sort(key=lambda s: s[0], reverse=True)
    base_cache: dict[int, float] = {}

    def _base(rid: int) -> float:
        if rid not in base_cache:
            base_cache[rid] = ros.weekly_lineup_total(scored_by_roster[rid], whole_slots, weeks)
        return base_cache[rid]

    def _after(rid: int, give: dict, receive: dict) -> float:
        roster = [p for p in scored_by_roster[rid] if p is not give] + [receive]
        return ros.weekly_lineup_total(roster, whole_slots, weeks) - _base(rid)

    proposals = []
    for _, other_id, give, receive in screened[:MAX_WEEKLY_RESCORES]:
        my_gain = round(_after(roster_id, give, receive), 1)
        if my_gain < bar:
            continue
        their_gain = round(_after(other_id, receive, give), 1)
        if their_gain < bar:
            continue
        timing = _timing([give], [receive], len(weeks))
        proposals.append({
            "partner_roster_id": other_id,
            "partner": names.get(other_id, f"Roster {other_id}"),
            "shape": "1-for-1",
            "you_give": _player_out(give),
            "you_get": _player_out(receive),
            "your_gain": my_gain,
            "their_gain": their_gain,
            "mutual_gain": round(my_gain + their_gain, 1),
            "your_gain_per_week": round(my_gain / max(1, len(weeks)), 2),
            # Sell-high / buy-low timing, apart from the lineup gain: the
            # ranking adds `timing.bonus`, the both-sides bar never does.
            "timing": timing,
            "rank_score": round(my_gain + timing["bonus"], 1),
        })

    proposals.sort(key=lambda p: (p["rank_score"], p["mutual_gain"]), reverse=True)
    top = _best_per_partner(proposals, limit)

    package_result = None
    if max_package_size and max_package_size > 1:
        package_result = _search_packages(
            rosters=rosters, roster_id=roster_id, scored_by_roster=scored_by_roster,
            whole_slots=whole_slots, weeks=weeks, wanted=wanted, bar=bar,
            names=names, roster_positions=league.get("roster_positions"),
            max_package_size=max_package_size, base=_base,
        )
    package_top = _best_per_partner(package_result["proposals"], limit) if package_result else []
    elapsed = time.monotonic() - started

    levels = replacement_levels(mine_scored, fractional_slots, whole_slots)
    message = (
        f"{len(top)} one-for-one trade(s) listed (best per partner, of {len(proposals)} found) "
        f"that improve both rosters over weeks {weeks[0]}-{weeks[-1]}." if top else
        f"No one-for-one trade improves both your roster and a partner's over the "
        f"rest of the season ({evaluated} candidate swaps checked)."
    )
    if package_result is not None:
        message += (
            f" {len(package_top)} package trade(s) listed (of {len(package_result['proposals'])} found)."
            if package_top else
            f" No package trade (up to {max_package_size} players a side) improves both rosters "
            f"({package_result['screened']} packages screened).")
    if deadline["urgent"]:
        message += f" {deadline['message']}"
    return create_success_response({
        "league": header,
        "season": season,
        "week": week,
        "roster_id": roster_id,
        "horizon": "ros",
        "weeks_scored": weeks,
        "playoff_weeks": meta["windows"]["playoff"],
        "trade_deadline": deadline,
        "data_freshness": freshness,
        "stale_data_warnings": _staleness_warnings(freshness),
        # Season-total ROS points of the weakest starter per position.
        "your_replacement_levels": {k: round(v, 1) for k, v in levels.items()},
        "proposals": top,
        "package_proposals": package_top,
        "max_package_size": max_package_size,
        # Every swap scored, not only the ones that survived.
        "candidates_considered": evaluated,
        "rescored_weekly": min(len(screened), MAX_WEEKLY_RESCORES),
        "proposals_found": len(proposals),
        "proposals_listed": len(top),
        "package_search": ({k: v for k, v in package_result.items() if k != "proposals"}
                           if package_result else None),
        "skipped_over_depth": skipped_depth,
        "minimum_gain": round(bar, 1),
        "schedule_unknown_weeks": meta["schedule_unknown_weeks"],
        "timing": {"projection_seconds": round(projected_at - started, 2),
                   "search_seconds": round(elapsed - (projected_at - started), 2),
                   "elapsed_seconds": round(elapsed, 2)},
        "method": (
            "one-for-one swaps (and, with max_package_size >= 2, packages built "
            "from each side's surplus plus at most one starter) scored by "
            "re-optimising both teams' best legal lineup for every remaining "
            "week (regular season and fantasy playoffs) on rest-of-season "
            f"projections and summing; only trades where BOTH sides gain at "
            f"least {round(bar, 1)} points are kept. Ranked by your gain plus a "
            "small sell-high / buy-low timing bonus (`timing`), shown apart."
        ),
        "caveats": [
            "Gains are rest-of-season lineup points (byes and expected injury "
            "absences included) — check the deal with analyze_trade before "
            "sending it.",
            "Packages receiving more players than they send drop the receiver's "
            "least valuable active player(s) (`your_drops` / `their_drops`) when "
            "the roster is full; kickers and defenses are not considered as drops.",
            "value_trajectory is an estimate of where trade value is headed, "
            "not a change to the ROS points the gains are scored on.",
            "Injured players are included and valued for the weeks they are "
            "expected back; the return window is an estimate (see get_ros_projections).",
        ],
        "message": message,
    })


def _best_per_partner(proposals: list[dict], limit: int) -> list[dict]:
    """One proposal per partner, best first: a set of conversations to have
    rather than twenty variations on the same one."""
    seen: set[int] = set()
    top = []
    for proposal in proposals:
        if proposal["partner_roster_id"] in seen:
            continue
        seen.add(proposal["partner_roster_id"])
        top.append(proposal)
        if len(top) >= limit:
            break
    return top


def _search_packages(
    *, rosters: list[dict], roster_id: int, scored_by_roster: dict[int, list[dict]],
    whole_slots: dict[str, int], weeks: list[int], wanted: set[str], bar: float,
    names: dict[int, str], roster_positions: list[str] | None, max_package_size: int,
    base,
) -> dict:
    """Package trades, screened then re-scored like the one-for-one search.

    Pruning keeps it bounded: each side builds packages only from its surplus
    (players who start in fewer than `STARTER_WEEK_SHARE` of the weeks) plus at
    most one starter, the top few of each per roster; a received package must
    hold someone who beats the receiver's weakest starter at his position
    (`lineup_bars`), or it cannot raise that lineup; and the screen stops at
    `MAX_PACKAGE_SCREENS`. A package that drops back to a smaller deal with
    both sides as well off (padding) is skipped. A side that receives more
    players than it sends drops its least valuable active players when its
    roster is full, and the lineup it is scored on is the one after the drop.
    """
    import time

    from . import ros
    from .roster_needs import lineup_bars

    started = time.monotonic()
    shapes = package_shapes(max_package_size)
    sizes = sorted({n for shape in shapes for n in shape})
    free = {r["roster_id"]: _free_slots(r, roster_positions) for r in rosters}
    n_weeks = len(weeks)

    pools: dict[int, dict[int, list[tuple[dict, ...]]]] = {}
    bars: dict[int, dict[str, float]] = {}
    totals: dict[int, float] = {}
    for rid, players in scored_by_roster.items():
        if not players:
            continue
        starts = week_starts(players, whole_slots, weeks)
        surplus, starters = package_pool(players, starts, n_weeks)
        pools[rid] = {n: packages(surplus, starters, n) for n in sizes}
        bars[rid] = lineup_bars(players, whole_slots)
        totals[rid] = starting_lineup_total(players, whole_slots)

    screens: dict[tuple, tuple[float, list[dict], list[dict]]] = {}

    def _screen(rid: int, give: tuple, get: tuple) -> tuple[float, list[dict], list[dict]]:
        key = (rid, tuple(sorted(id(p) for p in give)), tuple(sorted(id(p) for p in get)))
        if key not in screens:
            after, drops = roster_after(scored_by_roster[rid], give, get, free.get(rid, 0))
            screens[key] = (starting_lineup_total(after, whole_slots) - totals[rid], after, drops)
        return screens[key]

    def _upgrades(rid: int, pkg: tuple) -> bool:
        return any(float(p["total_points"]) > bars[rid].get(p["position"], 0.0) for p in pkg)

    def _padding(other: int, give: tuple, get: tuple, mine: float, theirs: float) -> bool:
        smaller = [(give[:i] + give[i + 1:], get) for i in range(len(give)) if len(give) > 1]
        smaller += [(give, get[:i] + get[i + 1:]) for i in range(len(get)) if len(get) > 1]
        for g, r in smaller:
            if (_screen(roster_id, g, r)[0] >= mine - PACKAGE_REDUNDANCY_EPS
                    and _screen(other, r, g)[0] >= theirs - PACKAGE_REDUNDANCY_EPS):
                return True
        return False

    mine_pool = pools.get(roster_id) or {}
    candidates = []
    screened = 0
    capped = False
    for other, their_pool in pools.items():
        if other == roster_id or capped:
            continue
        for give_n, get_n in shapes:
            gives = [g for g in mine_pool.get(give_n, []) if _upgrades(other, g)]
            gets = [r for r in their_pool.get(get_n, [])
                    if all(p["position"] in wanted for p in r) and _upgrades(roster_id, r)]
            for give in gives:
                for get in gets:
                    if screened >= MAX_PACKAGE_SCREENS:
                        capped = True
                        break
                    screened += 1
                    mine, my_after, _ = _screen(roster_id, give, get)
                    if mine <= 0 or _pile_up(my_after, scored_by_roster[roster_id], whole_slots):
                        continue
                    theirs = _screen(other, get, give)[0]
                    if theirs <= 0:
                        continue
                    candidates.append((mine + theirs, other, give, get, mine, theirs))
                if capped:
                    break

    candidates.sort(key=lambda c: c[0], reverse=True)
    proposals = []
    rescored = padding = 0
    for _, other, give, get, mine, theirs in candidates:
        if rescored >= MAX_PACKAGE_RESCORES:
            break
        if _padding(other, give, get, mine, theirs):
            padding += 1
            continue
        rescored += 1
        _, my_after, my_drops = _screen(roster_id, give, get)
        _, their_after, their_drops = _screen(other, get, give)
        my_gain = round(ros.weekly_lineup_total(my_after, whole_slots, weeks) - base(roster_id), 1)
        if my_gain < bar:
            continue
        their_gain = round(ros.weekly_lineup_total(their_after, whole_slots, weeks) - base(other), 1)
        if their_gain < bar:
            continue
        timing = _timing(list(give), list(get), n_weeks)
        proposals.append({
            "partner_roster_id": other,
            "partner": names.get(other, f"Roster {other}"),
            "shape": f"{len(give)}-for-{len(get)}",
            "you_give": [_player_out(p) for p in give],
            "you_get": [_player_out(p) for p in get],
            # Cut to make room (a full roster receiving more than it sends).
            "your_drops": [{"name": p["name"], "position": p["position"],
                            "total_points": p["total_points"]} for p in my_drops],
            "their_drops": [{"name": p["name"], "position": p["position"],
                             "total_points": p["total_points"]} for p in their_drops],
            "your_gain": my_gain,
            "their_gain": their_gain,
            "mutual_gain": round(my_gain + their_gain, 1),
            "your_gain_per_week": round(my_gain / max(1, n_weeks), 2),
            "timing": timing,
            "rank_score": round(my_gain + timing["bonus"], 1),
        })
    proposals.sort(key=lambda p: (p["rank_score"], p["mutual_gain"]), reverse=True)
    return {
        "proposals": proposals,
        "shapes": [f"{g}-for-{r}" for g, r in shapes],
        "screened": screened,
        "screen_capped": capped,
        "passed_screen": len(candidates),
        "skipped_padding": padding,
        "rescored_weekly": rescored,
        "found": len(proposals),
        "search_seconds": round(time.monotonic() - started, 2),
    }
