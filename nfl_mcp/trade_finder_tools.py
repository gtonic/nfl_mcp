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

Deliberately one-for-one. Multi-player packages explode the search space and are
better handed to `analyze_trade` once the shape of the deal is known; a
one-for-one that helps both rosters is the honest starting point for the
conversation.
"""
from __future__ import annotations

import logging

from .briefing_tools import _staleness_warnings
from .database import NFLDatabase
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
) -> dict:
    """Find one-for-one trades that improve both your lineup and theirs."""
    from . import ros, sleeper_tools
    from .briefing_tools import _scoring_label, _scoring_ppr
    from .projections import project_players
    from .scoring import league_scoring

    db = NFLDatabase()

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
    rosters = (rosters_resp or {}).get("rosters") or []
    if roster_id is None:
        if not user_id:
            return create_success_response({
                "success": False,
                "error": "Pass roster_id or user_id to identify which team to advise.",
            })
        mine = next((r for r in rosters if r.get("owner_id") == user_id), None)
    else:
        mine = next((r for r in rosters if r.get("roster_id") == roster_id), None)
    if not mine:
        return create_success_response({
            "success": False,
            "error": f"No roster found in league {league_id} for the given identifier.",
        })
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
        return await _find_ros(
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
        )

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
    for other_id, their_scored in scored_by_roster.items():
        if other_id == roster_id or not their_scored:
            continue
        their_candidates = _top_candidates(their_scored)
        for give in my_candidates:
            for receive in their_candidates:
                if receive["position"] not in wanted:
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

    levels = replacement_levels(mine_scored, fractional_slots)

    return create_success_response({
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
        "proposals_found": len(proposals),
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
            f"{len(top)} trade(s) that improve both rosters in week {week}."
            if top else
            f"No one-for-one trade improves both your roster and a partner's in "
            f"week {week} ({evaluated} candidate swaps checked)."
        ) + (f" {deadline['message']}" if deadline["urgent"] else ""),
    })


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


async def _find_ros(
    *, db, league: dict, league_id: str, rosters: list[dict], roster_id: int,
    season: int, week: int, positions: list[str] | None, limit: int,
    names: dict[int, str], deadline: dict, freshness: dict, header: dict,
    whole_slots: dict[str, int], fractional_slots: dict[str, float],
) -> dict:
    """The rest-of-season search: every week's best lineup, before and after.

    Two passes, because re-optimising ~15 weekly lineups for every one of
    ~1,500 swaps is too slow to run per request. A season-total lineup
    (each player at his summed ROS points) screens the swaps; the ones that
    help both sides on that screen are re-scored week by week, which is where
    a bye or an injury absence covered by depth shows up.
    """
    from . import ros

    # Reserve players are tradeable and counted: their ROS already carries the
    # expected absence, and the weeks after it are exactly what a trade buys.
    ids_by_roster: dict[int, list[str]] = {}
    for roster in rosters:
        taxi = {str(p) for p in (roster.get("taxi") or [])}
        ids_by_roster[roster["roster_id"]] = [
            str(p) for p in (roster.get("players") or []) if str(p) not in taxi
        ]
    all_ids = [pid for ids in ids_by_roster.values() for pid in ids]
    by_id, meta = await ros.ros_for_ids(all_ids, league=league, season=season,
                                        week=week, db=db)
    weeks = sorted(set(meta["windows"]["regular"]) | set(meta["windows"]["playoff"]))

    def _scored(ids: list[str]) -> list[dict]:
        out = []
        for pid in ids:
            e = by_id.get(pid)
            if not e or e["position"] not in TRADEABLE_POSITIONS:
                continue
            out.append({**e, "name": e["player"],
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
    for other_id, their_scored in scored_by_roster.items():
        if other_id == roster_id or not their_scored:
            continue
        for give in my_candidates:
            for receive in _top(their_scored):
                if receive["position"] not in wanted:
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
        proposals.append({
            "partner_roster_id": other_id,
            "partner": names.get(other_id, f"Roster {other_id}"),
            "you_give": {k: give.get(k) for k in _ROS_FIELDS},
            "you_get": {k: receive.get(k) for k in _ROS_FIELDS},
            "your_gain": my_gain,
            "their_gain": their_gain,
            "mutual_gain": round(my_gain + their_gain, 1),
            "your_gain_per_week": round(my_gain / max(1, len(weeks)), 2),
        })

    proposals.sort(key=lambda p: (p["your_gain"], p["mutual_gain"]), reverse=True)
    seen: set[int] = set()
    top = []
    for proposal in proposals:
        if proposal["partner_roster_id"] in seen:
            continue
        seen.add(proposal["partner_roster_id"])
        top.append(proposal)
        if len(top) >= limit:
            break

    levels = replacement_levels(mine_scored, fractional_slots)
    message = (
        f"{len(top)} trade(s) that improve both rosters over weeks "
        f"{weeks[0]}-{weeks[-1]}." if top else
        f"No one-for-one trade improves both your roster and a partner's over the "
        f"rest of the season ({evaluated} candidate swaps checked)."
    )
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
        # Every swap scored, not only the ones that survived.
        "candidates_considered": evaluated,
        "rescored_weekly": min(len(screened), MAX_WEEKLY_RESCORES),
        "proposals_found": len(proposals),
        "minimum_gain": round(bar, 1),
        "schedule_unknown_weeks": meta["schedule_unknown_weeks"],
        "method": (
            "one-for-one swaps scored by re-optimising both teams' best legal "
            "lineup for every remaining week (regular season and fantasy "
            "playoffs) on rest-of-season projections and summing; only trades "
            f"where BOTH sides gain at least {round(bar, 1)} points are kept"
        ),
        "caveats": [
            "Gains are rest-of-season lineup points (byes and expected injury "
            "absences included) — check the deal with analyze_trade before "
            "sending it.",
            "One-for-one only; a package deal is a different search.",
            "Injured players are included and valued for the weeks they are "
            "expected back; the return window is an estimate (see get_ros_projections).",
        ],
        "message": message,
    })
