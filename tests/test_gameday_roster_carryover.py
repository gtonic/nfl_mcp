"""ESPN game rosters carry the previous game's ``didNotPlay`` flags.

Observed live on 2026-10-18 at 16:31 UTC (T-29, week 6, CAR at PHI, event
401872998, PHI = ESPN team 21): the roster flagged the six real inactives
(Barkley, Hunt, M. Carter II, Epenesa, Brown, Milton III) plus Lane Johnson,
and also Epps, Greenard, D. Smith and Kendall -- exactly week 5's flags (event
401872981), all four active in week 6. The entries carry nothing else that
tells the two apart (``active`` / ``starter`` / ``valid`` read the same). The
ESPN injury list's athletes have no ``id``, only player-card links, so
"Hollywood Brown"'s note did not join Sleeper's "Marquise Brown" row.
Shapes below are trimmed copies of those payloads.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from nfl_mcp import gameday_inactives as gi
from nfl_mcp import tool_registry

KICKOFF = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
NOW = KICKOFF - timedelta(minutes=29)
WEEK6, WEEK5 = "401872998", "401872981"


def entry(pid, name, jersey, dnp):
    return {"playerId": pid, "displayName": name, "jersey": jersey, "active": False,
            "starter": False, "valid": not dnp, "didNotPlay": dnp, "period": 0, "forPlayerId": 0}


WEEK5_FLAGS = {15797: "L. Johnson", 3139368: "Epps", 3916409: "Greenard", 3929630: "Barkley",
               4241372: "Brown", 4241478: "D. Smith", 4567802: "Kendall"}
WEEK6_NEW = {4240456: "M. Carter II", 4240585: "Epenesa", 4360698: "Milton III", 4571162: "Hunt"}
PHI_WEEK6 = {"entries": [entry(3121023, "Goedert", "88", False),
                         *(entry(p, n, "0", True) for p, n in {**WEEK5_FLAGS, **WEEK6_NEW}.items())]}
PHI_WEEK5 = {"entries": [entry(3121023, "Goedert", "88", False),
                         *(entry(p, n, "0", True) for p, n in WEEK5_FLAGS.items())]}


def athlete(name, espn_id):
    slug = name.lower().replace(" ", "-").replace(".", "")
    return {"displayName": name, "links": [
        {"rel": ["playercard", "desktop", "athlete"],
         "href": f"https://www.espn.com/nfl/player/_/id/{espn_id}/{slug}"}]}


def item(name, espn_id, status, date, comment=""):
    return {"status": status, "date": date, "shortComment": comment,
            "athlete": athlete(name, espn_id)}


INJURIES = {"injuries": [{"displayName": "Philadelphia Eagles", "injuries": [
    item("AJ Epenesa", 4240585, "Out", "2026-10-18T15:49Z",
         "Epenesa (back) is inactive for Sunday's game against the Panthers."),
    item("Hollywood Brown", 4241372, "Out", "2026-10-18T15:45Z",
         "Brown (coach's decision) is inactive for Sunday's game against the Panthers."),
    item("Joe Milton", 4360698, "Out", "2026-10-18T15:43Z",
         "Milton (coach's decision) is inactive but will serve as the emergency third quarterback."),
    item("Lane Johnson", 15797, "Out", "2026-10-18T15:34Z", "out"),
    item("Jalyx Hunt", 4571162, "Out", "2026-10-18T15:33Z", "inactive"),
    item("Saquon Barkley", 3929630, "Out", "2026-10-18T15:33Z", "inactive"),
    item("Michael Carter II", 4240456, "Out", "2026-10-18T15:33Z", "inactive"),
    item("Drew Kendall", 4567802, "Questionable", "2026-10-17T20:00Z", "Kendall (knee) is questionable."),
    item("Jonathan Greenard", 3916409, "Active", "2026-10-17T02:27Z",
         "Greenard (ankle) does not carry an injury designation into Sunday's game against Carolina."),
    item("DeVonta Smith", 4241478, "Active", "2026-10-16T20:25Z",
         "Smith (hamstring) practiced in full Friday and doesn't have a designation for Sunday's game."),
]}]}

REAL = {"Saquon Barkley", "Jalyx Hunt", "Michael Carter II", "AJ Epenesa", "Marquise Brown",
        "Joe Milton III", "Lane Johnson"}
CARRIED_HEALTHY = {"Marcus Epps", "Jonathan Greenard", "DeVonta Smith", "Drew Kendall"}


def phi_db():
    db = MagicMock()
    db.get_week_kickoffs = MagicMock(return_value={"PHI": KICKOFF.isoformat(),
                                                    "CAR": KICKOFF.isoformat()})
    db.get_week_espn_ids = MagicMock(side_effect=lambda season, week: {
        6: {"PHI": {"event_id": WEEK6, "team_id": "21"}, "CAR": {"event_id": WEEK6, "team_id": "29"}},
        5: {"PHI": {"event_id": WEEK5, "team_id": "21"}, "CAR": {"event_id": "401872980", "team_id": "29"}},
    }.get(week, {}))
    sleeper = {"15797": ("S1", "Lane Johnson", "OT"), "3139368": ("S2", "Marcus Epps", "S"),
               "3916409": ("S3", "Jonathan Greenard", "DE"), "3929630": ("S4", "Saquon Barkley", "RB"),
               "4241372": ("S5", "Marquise Brown", "WR"), "4241478": ("S6", "DeVonta Smith", "WR"),
               "4567802": ("S7", "Drew Kendall", "C"), "4240456": ("S8", "Michael Carter II", "RB"),
               "4240585": ("S9", "AJ Epenesa", "DE"), "4360698": ("S10", "Joe Milton III", "QB"),
               "4571162": ("S11", "Jalyx Hunt", "LB"), "3121023": ("S12", "Dallas Goedert", "TE")}
    db.get_athletes_by_espn_ids = MagicMock(return_value={
        k: {"id": v[0], "full_name": v[1], "position": v[2]} for k, v in sleeper.items()})
    db.get_athletes_by_team = MagicMock(return_value=[])
    return db


def client_for(calls, phi_week6=PHI_WEEK6, prior_status=200, injuries=INJURIES):
    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if f"events/{WEEK5}" in url and "competitors/21/roster" in url:
            return httpx.Response(prior_status, json=PHI_WEEK5)
        if f"events/{WEEK6}" in url and "competitors/21/roster" in url:
            return httpx.Response(200, json=phi_week6)
        if "/roster" in url:
            return httpx.Response(404, json={"error": {"code": 404}})
        if "sleeper" in url:
            return httpx.Response(200, json={})
        if "injuries" in url:
            return httpx.Response(200, json=injuries)
        return httpx.Response(200, json={"events": []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_espn_id_from_player_card_links():
    assert gi.espn_athlete_id(athlete("Hollywood Brown", 4241372)) == "4241372"
    assert gi.espn_athlete_id({"id": 7}) == "7"
    assert gi.espn_athlete_id({"uid": "s:20~l:28~a:15797"}) == "15797"
    assert gi.espn_athlete_id({"displayName": "X"}) is None


async def test_phi_week6_carried_over_flags_are_not_official():
    calls: list[str] = []
    async with client_for(calls) as client:
        out = await gi.get_official_inactives(phi_db(), 2026, 6, now=NOW, client=client)
    official = [r["player_name"] for r in out["inactives"] if r["team_id"] == "PHI"]
    # One row per player: the "Hollywood Brown" note joined "Marquise Brown".
    assert sorted(official) == sorted(REAL)
    assert len(official) == len(set(official))
    unconfirmed = {r["player_name"]: r for r in out["unconfirmed_inactives"]}
    assert set(unconfirmed) == CARRIED_HEALTHY
    assert all(r["official"] is False for r in unconfirmed.values())
    assert unconfirmed["DeVonta Smith"]["injury_status"] == "active"
    # Neither inactive nor declared active: their status is not known.
    active = {r["player_name"] for r in out["roster_active"]["PHI"]}
    assert active == {"Dallas Goedert"}
    index = gi.gameday_index(out)
    from nfl_mcp.opportunity_tools import norm_name
    assert (norm_name("DeVonta Smith"), "PHI") not in index
    assert index[(norm_name("Saquon Barkley"), "PHI")]["status"] == "inactive"


async def test_an_active_note_beats_a_carried_flag():
    injuries = {"injuries": [{"displayName": "Philadelphia Eagles", "injuries": [
        *INJURIES["injuries"][0]["injuries"],
        item("Drew Kendall", 4567802, "Out", "2026-10-18T16:32Z",
             "Kendall (knee) is active for Sunday's game against the Panthers."),
    ]}]}
    async with client_for([], injuries=injuries) as client:
        out = await gi.get_official_inactives(phi_db(), 2026, 6, now=NOW + timedelta(minutes=2),
                                              client=client)
    assert "Drew Kendall" not in {r["player_name"] for r in out["inactives"]}
    assert "Drew Kendall" in {r["player_name"] for r in out["unconfirmed_inactives"]}


async def test_unknown_previous_game_needs_backing_for_every_flag():
    # A healthy scratch nobody reported, new this week: fine when the previous
    # game is known, unconfirmed when it is not.
    week6 = {"entries": [*PHI_WEEK6["entries"], entry(9999, "Nobody", "99", True)]}
    async with client_for([], phi_week6=week6) as client:
        known = await gi.get_official_inactives(phi_db(), 2026, 6, now=NOW, client=client)
    assert "Nobody" in {r["player_name"] for r in known["inactives"]}
    gi.clear_caches()
    async with client_for([], phi_week6=week6, prior_status=500) as client:
        unknown = await gi.get_official_inactives(phi_db(), 2026, 6, now=NOW, client=client)
    assert {r["player_name"] for r in unknown["inactives"] if r["team_id"] == "PHI"} == REAL
    assert {r["player_name"] for r in unknown["unconfirmed_inactives"]} == CARRIED_HEALTHY | {"Nobody"}


async def test_week_one_has_no_previous_game():
    db = phi_db()
    db.get_week_espn_ids = MagicMock(side_effect=lambda season, week: {
        "PHI": {"event_id": WEEK6, "team_id": "21"}} if week == 1 else {})
    async with client_for([]) as client:
        out = await gi.get_official_inactives(db, 2026, 1, now=NOW, client=client)
    assert out["unconfirmed_inactives"] == []
    assert len([r for r in out["inactives"] if r["team_id"] == "PHI"]) == 11


async def test_previous_week_ids_from_the_scoreboard():
    db = phi_db()
    db.get_week_espn_ids = MagicMock(side_effect=lambda season, week: {
        "PHI": {"event_id": WEEK6, "team_id": "21"}} if week == 6 else {})
    scoreboard = {"events": [{"id": WEEK5, "date": "2026-10-11T17:00Z", "competitions": [
        {"competitors": [{"team": {"abbreviation": "PHI", "id": "21"}},
                         {"team": {"abbreviation": "JAX", "id": "30"}}]}]}]}
    calls: list[str] = []
    inner = client_for(calls)

    async def handler(request: httpx.Request) -> httpx.Response:
        if "scoreboard" in str(request.url):
            return httpx.Response(200, json=scoreboard)
        return await inner._transport.handle_async_request(request)
    async with inner, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await gi.get_official_inactives(db, 2026, 6, now=NOW, client=client)
    assert {r["player_name"] for r in out["unconfirmed_inactives"]} == CARRIED_HEALTHY


async def test_tool_lists_unconfirmed_flags_as_not_official():
    async with client_for([]) as client:
        official = await gi.get_official_inactives(phi_db(), 2026, 6, now=NOW, client=client)
    with patch("nfl_mcp.tools._context.get_db", return_value=MagicMock()), \
         patch("nfl_mcp.gameday_inactives.get_official_inactives", AsyncMock(return_value=official)), \
         patch("nfl_mcp.injury_service.get_injury_reports", AsyncMock(return_value=[])):
        out = await tool_registry.get_gameday_inactives(season=2026, week=6, teams=["PHI"])
    rows = {r["player_name"]: r for r in out["inactives"]}
    assert {n for n, r in rows.items() if r["official"]} == REAL
    assert rows["DeVonta Smith"]["official"] is False
    assert rows["DeVonta Smith"]["basis"] == "espn_game_roster_unconfirmed"
    assert "Hollywood Brown" not in rows
