"""Join injury reports to Sleeper players across the two id spaces.

`player_injuries` and `injury_history` carry ESPN
athlete ids; `athletes` and every roster carry Sleeper ids. The two are
unrelated numbers — Jayden Daniels is 4426348 in one and 11566 in the other —
and 12 of ~2600 report rows collide by accident with a *different* Sleeper
player (ESPN 8439 is Aaron Rodgers, Sleeper 8439 is Demetris Robertson). So a
lookup by id finds nothing for the player asked about and, occasionally, a
stranger's injury. The join has to go through (normalized name, team), using
the same normalization the nflverse lookup uses.
"""

from __future__ import annotations

import json

from .injury_service import worst_status
from .opportunity_tools import norm_name
from .teams import normalize_team


def sleeper_injury_status(row: dict | None) -> str | None:
    """Sleeper's injury status for an athlete row, or None.

    Read from the stored raw payload: the top-level `status` column carries
    roster status ("Active"/"Inactive"), which is a different question.
    """
    if not row:
        return None
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return (raw or {}).get("injury_status") if isinstance(raw, dict) else None


def _espn_id(row: dict | None) -> str | None:
    """The ESPN athlete id Sleeper's payload names for an athlete row, or None."""
    if not row:
        return None
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    value = (raw or {}).get("espn_id") if isinstance(raw, dict) else None
    return str(value) if value not in (None, "") else None


# Short first names the two feeds use for the same player (ESPN "Robert",
# Sleeper "Bobby"). The only first-name fallback find_report accepts: a generic
# prefix rule matched "Chris Smith" to "Christian Smith" and "Jay" to "Jaylon".
# "Stephen" and "Steven" are different names, not spellings of one. Used for
# matching only, never displayed.
_FIRST_NAME_ALIASES = {
    "zach": "zachary", "zack": "zachary", "zak": "zachary",
    "rob": "robert", "bob": "robert", "bobby": "robert", "robbie": "robert",
    "will": "william", "bill": "william", "billy": "william", "willie": "william",
    "josh": "joshua", "mike": "michael", "chris": "christopher", "matt": "matthew",
    "nick": "nicholas", "dan": "daniel", "danny": "daniel", "jon": "jonathan",
    "johnny": "jonathan", "ben": "benjamin", "alex": "alexander", "cam": "cameron",
    "ken": "kenneth", "kenny": "kenneth", "sam": "samuel", "sammy": "samuel",
    "tony": "anthony", "joe": "joseph", "joey": "joseph", "jake": "jacob",
    "dave": "david", "jim": "james", "jimmy": "james", "tom": "thomas",
    "tommy": "thomas", "tim": "timothy", "andy": "andrew", "drew": "andrew",
    "ed": "edward", "eddie": "edward", "rich": "richard", "rick": "richard",
    "ricky": "richard", "greg": "gregory", "pat": "patrick", "steve": "steven",
    "jeff": "jeffrey", "nate": "nathaniel", "fred": "frederick",
    "doug": "douglas", "gabe": "gabriel",
}


def _alias_key(name: str) -> str:
    """``norm_name`` output with the first name mapped onto its canonical form."""
    first, _, rest = name.partition(" ")
    return f"{_FIRST_NAME_ALIASES.get(first, first)} {rest}".strip()


class _TeamNames:
    """``team -> {normalized athlete names}`` from a database, read on demand.

    Only the alias fallback asks, so most indexes never touch the database.
    ``db=None`` uses the shared database.
    """

    def __init__(self, db):
        self._db = db
        self._cache: dict[str, set[str]] = {}

    def __call__(self, team: str) -> set[str]:
        if team not in self._cache:
            names: set[str] = set()
            try:
                db = self._db
                if db is None:
                    from .database import get_shared_db
                    db = get_shared_db()
                for row in db.get_athletes_by_team(team) or []:
                    if isinstance(row, dict) and (n := norm_name(row.get("full_name"))):
                        names.add(n)
            except Exception:
                names = set()
            self._cache[team] = names
        return self._cache[team]


def team_names_from(db) -> _TeamNames:
    """A ``team_names`` lookup for ``build_injury_index`` over ``db``."""
    return _TeamNames(db)


class _InjuryIndex(dict):
    """``{(normalized name, team): report}`` plus the fallback lookups.

    A plain dict to every caller; ``find_report`` also consults ``by_id``
    (ESPN id -> report) and ``aliases`` (canonical first name + team).
    ``team_names`` (team -> normalized athlete names), when set, lets the
    alias fallback see that a teammate owns the report's exact name.
    """

    def __init__(self):
        super().__init__()
        self.aliases: dict[tuple[str, str], list[dict]] = {}
        self.by_id: dict[str, dict] = {}
        self.team_names = None


def build_injury_index(injuries: list[dict], team_names=None) -> dict[tuple[str, str], dict]:
    """Index the stored (ESPN) injury reports by (normalized name, team).

    ``team_names``: optional ``team -> set of normalized athlete names``
    callable (see ``_InjuryIndex``).
    """
    index = _InjuryIndex()
    index.team_names = team_names or _TeamNames(None)
    for row in injuries:
        name = norm_name(row.get("player_name"))
        team = normalize_team(row.get("team_id")) or ""
        if row.get("player_id"):
            index.by_id[str(row["player_id"])] = row
        if name:
            index[(name, team)] = row
            index.aliases.setdefault((_alias_key(name), team), []).append(row)
    return index


def _same_position(athlete_row: dict | None, report: dict) -> bool:
    mine = ((athlete_row or {}).get("position") or "").upper()
    theirs = (report.get("position") or "").upper()
    return not (mine and theirs and mine != theirs)


def find_report(
    athlete_row: dict | None, injury_index: dict[tuple[str, str], dict], team: str | None
) -> dict | None:
    """The injury report row for a Sleeper athlete, or None.

    The ESPN id Sleeper names for the athlete first (an exact join). Then the
    exact normalized name on the team. Then, on the same team and only when
    exactly one report qualifies: the same canonical first name via a known
    nickname ("Bobby" = "Robert", "Zach" = "Zachary") -- refused when the
    positions contradict, when the report carries a different ESPN id than
    the athlete's, or when a teammate owns the report's exact name (brothers).
    Never a guess between two teammates, and no bare prefix rule: "Chris" is
    not "Christian", "Jay" is not "Jaylon".
    """
    name = norm_name((athlete_row or {}).get("full_name"))
    if not name:
        return None
    team_key = normalize_team(team) or ""
    espn_id = _espn_id(athlete_row)
    if espn_id and isinstance(injury_index, _InjuryIndex):
        by_id = injury_index.by_id.get(espn_id)
        if by_id is not None and (not team_key
                                  or (normalize_team(by_id.get("team_id")) or "") == team_key):
            return by_id
    hit = injury_index.get((name, team_key))
    if hit is not None or not isinstance(injury_index, _InjuryIndex) or not team_key:
        return hit
    rows = [r for r in injury_index.aliases.get((_alias_key(name), team_key), [])
            if _same_position(athlete_row, r)
            and not (espn_id and r.get("player_id") and str(r["player_id"]) != espn_id)
            and not _owned_by_teammate(injury_index, r, team_key)]
    return rows[0] if len(rows) == 1 else None


def _owned_by_teammate(index: _InjuryIndex, report: dict, team: str) -> bool:
    """Whether another athlete on ``team`` has the report's exact name."""
    lookup = index.team_names
    if lookup is None:
        return False
    try:
        names = lookup(team) or ()
    except Exception:
        return False
    return norm_name(report.get("player_name")) in names


def report_ids_for(athlete_rows: list[dict], injury_index: dict[tuple[str, str], dict]) -> list[str]:
    """ESPN report ids for Sleeper athletes — the ids `injury_history` is keyed by."""
    ids = []
    for row in athlete_rows:
        report = find_report(row, injury_index, row.get("team_id"))
        if report and report.get("player_id"):
            ids.append(str(report["player_id"]))
    return ids


def resolve_injury(
    athlete_row: dict | None, injury_index: dict[tuple[str, str], dict], team: str
) -> dict | None:
    """Combine Sleeper's status with the ESPN injury report, worst case wins.

    Returns ``{status, source, sleeper_status, report_status, injury_type}``
    or None when neither source says anything. Disagreement is the
    normal state around kickoff, not an anomaly: on 2026-09-20 ESPN had Brock
    Bowers at doubtful while Sleeper still said questionable, and taking the
    milder reading put a 0.9 multiplier on a player who did not play.
    """
    sleeper_status = sleeper_injury_status(athlete_row)
    report = find_report(athlete_row, injury_index, team)
    report_status = (report or {}).get("injury_status")
    # "Active" is the report's way of saying "no injury", so it must not beat a
    # real designation from the other source.
    if report_status == "Active":
        report_status = None

    status = worst_status(sleeper_status, report_status)
    if not status:
        return None
    return {
        "status": status,
        "source": ("both" if sleeper_status and report_status
                   else "report" if report_status else "sleeper"),
        "sleeper_status": sleeper_status,
        "report_status": report_status,
        "injury_type": (report or {}).get("injury_type"),
    }


# How old a stored report may be before a tool that was handed no status stops
# trusting it. Reports are cleared when a crawl no longer lists them, so a row
# is current as of the last crawl; this only guards against a prefetch that
# has not run for days.
LOOKUP_MAX_AGE_HOURS = 72


def lookup_injury(db, player_name: str | None, team: str | None) -> dict | None:
    """Current injury for a player known only by name and team, or None.

    For tools whose callers pass players as plain dicts: without this they
    project anyone they were not explicitly told about at full health, while
    the briefing — reading the same database — benches him.
    """
    team = normalize_team(team)
    if not db or not player_name or not team:
        return None
    report_index: dict[tuple[str, str], dict] = {}
    if hasattr(db, "find_player_injury"):
        report = db.find_player_injury(player_name, team, max_age_hours=LOOKUP_MAX_AGE_HOURS)
        if isinstance(report, dict):
            report_index = build_injury_index([report])
    athlete = {"full_name": player_name}
    if hasattr(db, "search_athletes_by_name"):
        wanted = norm_name(player_name)
        for row in db.search_athletes_by_name(player_name, limit=10) or []:
            if (isinstance(row, dict) and norm_name(row.get("full_name")) == wanted
                    and normalize_team(row.get("team_id")) == team):
                athlete = row
                break
    return resolve_injury(athlete, report_index, team)


def injury_for_row(row: dict, injury_index: dict[tuple[str, str], dict]) -> dict | None:
    """`resolve_injury` for an athlete row, using the row's own team."""
    return resolve_injury(row, injury_index, normalize_team(row.get("team_id")) or "")


def misses_this_week(status: str | None) -> bool:
    """Whether a status all but rules the player out of this week's game.

    Doubtful counts: it projects at 0.35 and rarely plays. A one-week projection
    of such a player says nothing about his value beyond this week, so tools
    that act on that projection for longer — a drop, a trade — must not.
    """
    from .projections import _injury_mult  # deferred: projections is heavy
    return bool(status) and _injury_mult(status) <= 0.35
