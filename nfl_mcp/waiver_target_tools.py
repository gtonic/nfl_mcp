"""Who to claim off waivers this week, for one specific roster.

The weekly cycle is lineup / waivers / trades. ``get_weekly_briefing`` answers
the lineup question and ``analyze_trade`` the trade question; the waiver question
had no tool. What existed answers something adjacent: ``get_trending_players`` is
league-agnostic (it reports what *everyone* is adding, including players already
rostered in your league), ``recommend_faab_bid`` needs you to already know the
name, and ``get_waiver_wire_dashboard`` reads the transaction log rather than the
pool.

This starts from the pool: every athlete nobody in the league rosters, projected
for the coming week in the league's own scoring, and scored by the only thing
that makes a claim worth making — how much better they are than the player they
would displace in your lineup.
"""
from __future__ import annotations

import logging

from .briefing_tools import _staleness_warnings
from .database import NFLDatabase
from .errors import create_success_response
from .injury_match import build_injury_index, injury_for_row, misses_this_week
from .player_values import get_values_service
from .roster_needs import (
    lineup_bars,
    lineup_gain,
    lineup_slots,
    slot_counts,
    starting_lineup,
    starting_lineup_total,
)
from .teams import normalize_team
from .trade_analyzer_tools import league_format_from_settings
from .waiver_rules import waiver_rules

logger = logging.getLogger(__name__)

# Positions a claim is ever made for. Kickers and defenses are included because
# streaming them is most of what waiver claims are actually spent on.
CLAIMABLE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Sleeper roster statuses that mean the player is not an option this week.
_INACTIVE_STATUSES = {"Inactive", "Non Football Injury", "Practice Squad"}

# Below this the "upgrade" is inside the noise of a weekly projection (MAE is
# ~5.8 points), so calling it an upgrade would be false precision.
_MEANINGFUL_UPGRADE = 1.5

# How a drop candidate is ranked: mostly by rest-of-season market value, partly
# by this week's projection. One bad week says little; a low market value says
# the league agrees he is replaceable.
_DROP_VALUE_WEIGHT = 0.6
_MAX_DROP_CANDIDATES = 5

# Roster slots that do not count against the active roster size.
_NON_ROSTER_SLOTS = {"IR", "TAXI"}


def _value_of(player: dict) -> float:
    return float(player.get("value") or 0.0)


def _keep_scores(players: list[dict]) -> dict[int, float]:
    """How much each player is worth keeping, 0-1, relative to his own roster."""
    top_value = max((_value_of(p) for p in players), default=0.0) or 1.0
    top_points = max((float(p.get("projected_points") or 0.0) for p in players),
                     default=0.0) or 1.0
    return {
        id(p): round(
            _DROP_VALUE_WEIGHT * _value_of(p) / top_value
            + (1 - _DROP_VALUE_WEIGHT) * float(p.get("projected_points") or 0.0) / top_points,
            3,
        )
        for p in players
    }


def _droppable(players: list[dict], slots: dict[str, int],
               protected_ids: set[str]) -> list[dict]:
    """Bench players who could go, least worth keeping first.

    Never anyone who starts in the best lineup or whom the manager has set as
    a starter (``protected_ids``) — the drop list used to be the five lowest
    projections, which put a FLEX starter worth 2249 on it — and never a
    player who projects low only because he misses this week: a zero for an
    Out starter says nothing about the rest of the season.
    """
    starting = {id(p) for p in starting_lineup(players, slots)}
    keep = _keep_scores(players)
    bench = [
        p for p in players
        if id(p) not in starting
        and str(p.get("player_id")) not in protected_ids
        and not misses_this_week(p.get("injury_status"))
    ]
    bench.sort(key=lambda p: (keep[id(p)], _value_of(p), p.get("projected_points") or 0.0))
    return [{**p, "keep_score": keep[id(p)]} for p in bench]


def _outvalues(target: dict, drop: dict) -> bool:
    """Whether a claim is worth more than the player it would cost.

    Rest-of-season value decides, because a drop is permanent and this week's
    edge is not. The one exception is a player the market does not price at
    all (a kicker, a defense, a deep bench body), who can go for any real
    lineup gain.
    """
    if _value_of(target) > _value_of(drop):
        return True
    return _value_of(drop) == 0 and target.get("upgrade_points", 0.0) >= _MEANINGFUL_UPGRADE


def _pair_drop(target: dict, players: list[dict], slots: dict[str, int],
               set_starters: set[str], open_spots: int) -> tuple[dict | None, str]:
    """The player to drop for `target`, or None and why not."""
    if open_spots > 0:
        return None, "Open roster spot — no drop needed."
    # Judged on the roster *after* the add: a starter he displaces at his own
    # position (the kicker a streamer replaces) becomes droppable; a set
    # starter elsewhere does not.
    same_position = {
        str(p.get("player_id")) for p in players if p.get("position") == target.get("position")
    }
    protected = set_starters - same_position
    for candidate in _droppable([*players, target], slots, protected):
        if candidate.get("player_id") == target.get("player_id"):
            continue
        if _outvalues(target, candidate):
            return ({k: candidate.get(k) for k in (
                "player_id", "name", "position", "projected_points", "value")},
                f"Drop {candidate['name']} (value {int(_value_of(candidate))}, "
                f"{candidate.get('projected_points')} pts) — worth less than "
                f"{target['name']} (value {int(_value_of(target))}).")
        break  # the least-valuable bench player is worth more; the rest are too
    return None, (
        f"Nobody on your bench is worth less than {target['name']} rest-of-season "
        "— a one-week pickup at best, not worth a permanent drop."
    )


def _is_claimable(row: dict) -> bool:
    position = (row.get("position") or "").upper()
    if position not in CLAIMABLE_POSITIONS:
        return False
    if (row.get("status") or "") in _INACTIVE_STATUSES:
        return False
    return bool(normalize_team(row.get("team_id")))


def _to_projection_input(
    row: dict, opponents: dict[str, str], injury_index: dict | None = None
) -> dict | None:
    team = normalize_team(row.get("team_id"))
    position = (row.get("position") or "").upper()
    if not team:
        return None
    # A team defense is named by its team, not by whatever placeholder the
    # athlete cache carries for it ("Player-CAR").
    name = team if position in ("DEF", "DST") else row.get("full_name")
    if not name:
        return None
    opponent = opponents.get(team)
    if not opponent:
        return None  # on bye, or the schedule cache has not reached this week
    player = {
        "name": name, "position": position, "team": team,
        "opponent": opponent, "player_id": row.get("id"),
    }
    # Same sources as the briefing, worst case wins. Without it every player
    # projected at full health: an Out free agent could rank as an upgrade and
    # an Out starter of yours set the bar a claim had to clear.
    injury = injury_for_row(row, injury_index or {})
    if injury:
        player["injury"] = {"status": injury["status"]}
    return player


async def get_waiver_targets(
    league_id: str,
    roster_id: int | None = None,
    user_id: str | None = None,
    week: int | None = None,
    season: int | None = None,
    positions: list[str] | None = None,
    limit: int = 12,
) -> dict:
    """Rank this league's free agents by how much they would upgrade your lineup."""
    from . import sleeper_tools
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
    slots = slot_counts(league.get("roster_positions"))
    rules = waiver_rules(league)
    is_faab = rules["waiver_type"] == "faab"
    # Sleeper waiver_type 0 is rolling priority: a successful claim moves you
    # to the back of the order, so every claim has a real cost.
    rolling = not is_faab and (league.get("settings") or {}).get("waiver_type") == 0

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

    # Everything anyone rosters is off the table — including other teams' IR and
    # taxi players, who are owned even though they cannot be started.
    taken: set[str] = set()
    for roster in rosters:
        for key in ("players", "reserve", "taxi"):
            taken.update(str(p) for p in (roster.get(key) or []))

    freshness = db.get_data_freshness()

    opponents: dict[str, str] = {}
    for team, opp in db.get_week_opponents(season, week).items():
        canon_team, canon_opp = normalize_team(team), normalize_team(opp)
        if canon_team and canon_opp:
            opponents[canon_team] = canon_opp
    if not opponents:
        return create_success_response({
            "success": False,
            "error": (f"No cached schedule for {season} week {week} — run the "
                      "prefetch (NFL_MCP_PREFETCH=1) or call get_team_schedule first."),
            "season": season, "week": week,
        })

    # Only positions this league actually starts. Offering a kicker to a league
    # with no K slot is not a marginal call, it is a wrong answer — and with an
    # empty replacement level every kicker looked like a +9.5 upgrade.
    startable = {p for p, count in slots.items() if count > 0}
    if positions:
        wanted = [p.upper() for p in positions]
    else:
        wanted = [p for p in CLAIMABLE_POSITIONS if p in startable]
    if not wanted:
        return create_success_response({
            "success": False,
            "error": (f"League {league_id} starts none of {CLAIMABLE_POSITIONS} — "
                      "nothing to claim for."),
        })
    pool_rows = [
        row for row in db.get_athletes_by_positions(wanted, exclude_ids=taken)
        if _is_claimable(row)
    ]
    injury_index = build_injury_index(db.get_all_current_injuries())
    pool_inputs = [
        p for row in pool_rows if (p := _to_projection_input(row, opponents, injury_index))
    ]

    # My own roster, for the bar a claim has to clear. Reserve/taxi are excluded
    # for the same reason as in the briefing: they cannot hold a starting slot.
    unavailable = {str(p) for p in (mine.get("reserve") or [])}
    unavailable |= {str(p) for p in (mine.get("taxi") or [])}
    my_rows = db.get_athletes_by_ids(
        [str(p) for p in (mine.get("players") or []) if str(p) not in unavailable]
    )
    my_inputs = [
        p for row in my_rows.values()
        if (p := _to_projection_input(row, opponents, injury_index))
    ]

    my_proj = await project_players(
        my_inputs, scoring=scoring_exact, num_teams=num_teams, season=season, week=week
    )
    pool_proj = await project_players(
        pool_inputs, scoring=scoring_exact, num_teams=num_teams, season=season, week=week
    )

    def _named(result, inputs):
        by_name = {(p["name"], p["team"]): p["player_id"] for p in inputs}
        status_of = {(p["name"], p["team"]): (p.get("injury") or {}).get("status") for p in inputs}
        return [
            {
                "player_id": by_name.get((p["player"], p["team"])),
                "injury_status": status_of.get((p["player"], p["team"])),
                "name": p["player"], "position": p["position"], "team": p["team"],
                "opponent": p["opponent"],
                "projected_points": p["projected_points"],
                "floor": p["floor"], "ceiling": p["ceiling"],
                "confidence": p["confidence"],
                "base_source": (p.get("breakdown") or {}).get("base_source"),
            }
            for p in (result or {}).get("projections") or []
        ]

    mine_scored = _named(my_proj, my_inputs)
    pool_scored = _named(pool_proj, pool_inputs)

    # Rest-of-season market value, so a drop weighs more than one week. The
    # answer without it is still useful, so a failed fetch only degrades.
    values_ok = False
    try:
        service = get_values_service(db)
        fmt = league_format_from_settings(league)
        values = await service.get_values(
            fmt["ppr"], fmt["num_qbs"], fmt["num_teams"], fmt["is_dynasty"])
        for p in (*mine_scored, *pool_scored):
            hit = service.lookup(values, player_id=p.get("player_id"),
                                 name=p.get("name"), position=p.get("position"))
            p["value"] = int(hit["value"]) if hit and hit.get("value") is not None else None
        values_ok = bool((values or {}).get("list"))
    except Exception as e:
        logger.warning(f"player values unavailable for waiver drops: {e}")
    whole_slots = lineup_slots(league.get("roster_positions"))
    base_total = starting_lineup_total(mine_scored, whole_slots)
    # The weakest player actually starting where each position could play.
    levels = lineup_bars(mine_scored, whole_slots)

    trending: dict[str, int] = {}
    try:
        trend = await sleeper_tools.get_trending_players(trend_type="add", limit=100)
        for entry in (trend or {}).get("trending_players") or []:
            pid = entry.get("player_id")
            if pid:
                trending[str(pid)] = int(entry.get("count") or 0)
    except Exception as e:  # additive signal; never fail the answer on it
        logger.debug(f"trending adds unavailable for waiver targets: {e}")

    # Without live Vegas lines, defenses and kickers are priced off a constant,
    # so every one of them projects identically and "ranking" them is noise.
    # Say so rather than emitting a confident order over indistinguishable rows.
    vegas_active = bool((pool_proj or {}).get("vegas_active"))
    undifferentiated = set() if vegas_active else {"K", "DEF", "DST"}

    targets = []
    for candidate in pool_scored:
        position = candidate["position"]
        level = levels.get(position, 0.0)
        # Scored on the whole lineup, FLEX included, rather than against the
        # per-position bar — see `lineup_gain`.
        upgrade = round(lineup_gain(mine_scored, whole_slots, candidate, base_total), 1)
        adds = trending.get(str(candidate.get("player_id")), 0)
        if position in undifferentiated:
            verdict = "no_signal"
        elif upgrade >= _MEANINGFUL_UPGRADE:
            verdict = "upgrade"
        # A projection this close is a coin flip, but a player the league is
        # adding in bulk is usually one carrying news we have not priced yet —
        # worth a speculative claim rather than a start. Only near replacement
        # level though: hype does not make a clearly worse player a claim.
        # The lineup gain never goes below zero, so "close" is still measured
        # against the position's bar.
        elif adds > 0 and candidate["projected_points"] - level >= -_MEANINGFUL_UPGRADE:
            verdict = "speculative"
        else:
            verdict = "no"
        targets.append({
            **candidate,
            "replacement_level": round(level, 1),
            "upgrade_points": upgrade,
            "trending_adds": adds,
            "verdict": verdict,
        })

    targets.sort(key=lambda t: (t["upgrade_points"], t["trending_adds"]), reverse=True)
    top = [t for t in targets if t["verdict"] in ("upgrade", "speculative")][:limit]

    # Who you would drop: bench players only, least worth keeping first. Stashed
    # players are left out — dropping an IR spot is a different decision.
    injured_held = [p for p in mine_scored if misses_this_week(p.get("injury_status"))]
    set_starters = {str(p) for p in (mine.get("starters") or []) if p}
    drops = _droppable(mine_scored, whole_slots, set_starters)[:_MAX_DROP_CANDIDATES]
    roster_size = sum(
        1 for slot in league.get("roster_positions") or [] if slot not in _NON_ROSTER_SLOTS
    )
    active_count = sum(1 for p in (mine.get("players") or []) if str(p) not in unavailable)
    open_spots = max(0, roster_size - active_count) if roster_size else 0
    for target in top:
        drop, note = _pair_drop(target, mine_scored, whole_slots, set_starters, open_spots)
        target["drop"] = drop
        target["drop_note"] = note

    empty_slots = sorted(
        position for position, count in slots.items()
        if count >= 1 and sum(1 for p in mine_scored if p["position"] == position) < round(count)
    )

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
        "waiver_type": rules["waiver_type"],
        # Whether you can add a player now or have to wait for the waiver run.
        # Inferring this from the raw settings is how it got got wrong before.
        "waiver_rules": rules,
        "pool_size": len(pool_scored),
        "data_freshness": freshness,
        "stale_data_warnings": _staleness_warnings(freshness),
        "positions_considered": sorted(wanted),
        "vegas_active": vegas_active,
        "warnings": ([] if values_ok else [
            "No market values available — drops are ranked by this week's "
            "projection only."
        ]) + ([] if vegas_active else [
            "No live Vegas lines (set ODDS_API_KEY) — defenses and kickers are "
            "priced off a constant, so they are reported as no_signal rather "
            "than ranked."
        ]),
        "replacement_levels": {k: round(v, 1) for k, v in levels.items()},
        "targets": top,
        # Bench players only (never a starter, never someone who misses just
        # this week), ranked by rest-of-season value and this week's
        # projection. Each target carries its own `drop`, paired only when he
        # is worth more than the player he costs.
        "drop_candidates": drops,
        "open_roster_spots": open_spots,
        # Out/doubtful players on your active roster, kept off the drop list.
        # Whether to hold, move to IR or cut them is not a one-week call.
        "injured_not_dropped": [
            {"name": p["name"], "position": p["position"], "injury_status": p["injury_status"]}
            for p in injured_held
        ],
        "thin_positions": empty_slots,
        "horizon": "this_week",
        "method": (
            "free agents (nobody in the league rosters them) projected for the "
            "coming week in this league's scoring, ranked by how many points they "
            "add to your best legal starting lineup (FLEX included)"
        ),
        "message": (
            (f"{len(top)} claim(s) worth making from {len(pool_scored)} free agents "
             f"in week {week}."
             if top else
             # An honest "nothing here" beats a ranked list of players who would
             # all make the lineup worse.
             f"Nothing on waivers beats your current starters in week {week} "
             f"({len(pool_scored)} free agents checked).")
            + ("" if is_faab else
               " Rolling waivers: a successful claim sends you to the back of the "
               "order — spend it on a real lineup upgrade, and add marginal players "
               "as free agents once they clear waivers."
               if rolling else " Priority waivers: spend position, not budget.")
        ),
    })
