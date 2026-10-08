"""Real practice participation (DNP/LP/FP), per report day.

Practice status used to be *invented*: the prefetch turned an injury
designation into a practice line (Questionable -> LP, Out -> DNP) and
enrichment filled any gap with a full practice. That told the user a player had
practised who never took the field, and fed the made-up line into the start/sit
health score. This module reads what was actually reported and nothing else.

Sources, checked against the live feeds on 2026-09-23:

- **NFL.com injury report** (``/injuries/league/{season}/reg{week}``) — the
  official league report, one table per team with the latest "Practice Status"
  ("Did Not Participate In Practice" / "Limited ..." / "Full ...") and, on the
  final report, the game designation. It shows only the *latest* day, so each
  snapshot is attributed to a report day (``report_day``) and the week's
  pattern is built from the stored snapshots. Primary.
- **ESPN news blurbs** (``site.api.espn.com/.../nfl/injuries``) — the RotoWire
  notes carry lines like "Taylor (ribs) was a full participant in Tuesday's
  practice". The day is named, so these date themselves. Fallback only: a
  blurb covers a few players, not the whole report.

Not sources: Sleeper's ``practice_participation`` / ``practice_description``
are on every player row but populated for 1 of 12,228 players (a 2017-era
record), and ESPN's injury detail objects have no practice fields at all.

When neither source says anything about a player, his practice status is
``None`` — "unreported", never "full".
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta, timezone

from .opportunity_tools import norm_name
from .teams import normalize_team

logger = logging.getLogger(__name__)

NFL_COM_URL = "https://www.nfl.com/injuries/league/{season}/reg{week}"
ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"

SOURCE_NFL_COM = "nfl.com"
SOURCE_ESPN_NEWS = "espn_news"
# Which source may overwrite which for the same player and day: the official
# report beats a news blurb about it.
SOURCE_RANK = {SOURCE_NFL_COM: 2, SOURCE_ESPN_NEWS: 1}

# Participation ladder used for the trend. A veteran rest day is a healthy
# player not practising, so it ranks with a full practice.
_LADDER = {"DNP": 0, "LP": 1, "FP": 2, "REST": 2}
_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Teams post their report mid-to-late afternoon Eastern. A snapshot taken
# earlier in the day still shows the previous day's report.
REPORT_CUTOFF_HOUR_ET = 16


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def to_eastern(now: datetime) -> datetime:
    """``now`` in US Eastern. Falls back to a fixed DST rule without tzdata."""
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # Slim images can lack the tz database. Mid-March to early November is
        # EDT, which covers the regular season up to week 9.
        dst = (3, 15) <= (now.month, now.day) < (11, 5)
        return now.astimezone(timezone(timedelta(hours=-4 if dst else -5)))


def practice_week_start(today: date, include_monday: bool = False) -> date:
    """First day that can belong to the current practice week.

    Tuesday by default: Sunday teams report from Wednesday, and anything from
    last Thursday or Friday is last week's. ``include_monday`` also admits the
    short-week teams' Monday (estimated) report, for sources that date each
    note themselves.
    """
    start_wd = 0 if include_monday else 1
    return today - timedelta(days=(today.weekday() - start_wd) % 7)


def last_report_day(game_date: date) -> date:
    """The final practice-report day before a game.

    Friday for Sunday games, Saturday for Monday games, Wednesday for Thursday
    games (the short week's report runs Mon-Wed).
    """
    return game_date - timedelta(days=1 if game_date.weekday() == 3 else 2)


def report_day(now: datetime, game_date: date | None) -> date | None:
    """The report day a snapshot of the *latest* report taken at ``now`` shows.

    None when the snapshot cannot be this week's report: before the first
    report day of the week (the page is still last week's, or empty). After the
    final report the page keeps showing it, so later snapshots are clamped onto
    the final day rather than inventing a Saturday practice.
    """
    et = to_eastern(now)
    day = et.date() if et.hour >= REPORT_CUTOFF_HOUR_ET else et.date() - timedelta(days=1)
    if game_date is None:
        return None if day.weekday() == 6 else day
    last = last_report_day(game_date)
    if day > last:
        day = last
    if day < game_date - timedelta(days=4):
        return None
    return day


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
# Only rest or personal wording is a healthy day off. "Not Injury Related -
# Illness" is a sick player and stays a DNP.
_REST_RE = re.compile(r"\brest\b|\bresting\b|\bpersonal\b|\bveteran\b", re.I)


def normalize_practice(text: str | None, injury: str | None = None) -> str | None:
    """``DNP``/``LP``/``FP``/``REST`` for a practice description, or None.

    ``injury`` is the report's injury column: a DNP listed as "Not Injury
    Related - Rest" is a veteran day off, not a health concern.
    """
    s = (text or "").strip().lower()
    if not s:
        return None
    if s in ("dnp",) or "did not participate" in s or "did not practice" in s \
            or "non-participant" in s or "not participate" in s:
        return "REST" if injury and _REST_RE.search(injury) else "DNP"
    if s in ("lp", "limited") or "limited" in s:
        return "LP"
    if s in ("fp", "full") or "full participation" in s or "full participant" in s:
        return "FP"
    return None


# ---------------------------------------------------------------------------
# NFL.com official report
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}
_HEADER_DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:ST|ND|RD|TH)?", re.I)


def _header_date(text: str, season: int) -> date | None:
    """``"SUNDAY, SEPTEMBER 27TH"`` -> date. January/February games belong to
    the following calendar year."""
    parts = text.split(",", 1)
    m = _HEADER_DATE_RE.search(parts[1] if len(parts) > 1 else text)
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    month = _MONTHS[m.group(1).lower()]
    year = season + 1 if month <= 2 else season
    try:
        return date(year, month, int(m.group(2)))
    except ValueError:
        return None


def parse_nfl_com_report(html: str, season: int) -> list[dict]:
    """Rows of the NFL.com weekly injury report page.

    Returns ``{team, game_date, player_name, position, injury,
    practice_description, practice_status, game_status}`` for every listed
    player, in page order. Players with an unreadable practice cell are kept
    with ``practice_status`` None so the game designation is not lost.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict] = []
    game_date: date | None = None
    team: str | None = None
    for node in soup.find_all(["h2", "div", "table"]):
        classes = node.get("class") or []
        if node.name == "h2" and "d3-o-section-title" in classes:
            game_date = _header_date(node.get_text(" ", strip=True), season)
        elif node.name == "div" and "d3-o-section-sub-title" in classes:
            # The nickname ("Lions") identifies the team uniquely.
            team = normalize_team(node.get_text(" ", strip=True))
        elif node.name == "table" and "d3-o-reports--detailed" in classes:
            if not team:
                continue
            for tr in node.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(cells) < 4 or not cells[0]:
                    continue
                name, position, injury, practice = cells[:4]
                game_status = cells[4] if len(cells) > 4 else ""
                rows.append({
                    "team": team,
                    "game_date": game_date.isoformat() if game_date else None,
                    "player_name": " ".join(name.split()),
                    "position": position or None,
                    "injury": injury or None,
                    "practice_description": practice or None,
                    "practice_status": normalize_practice(practice, injury),
                    "game_status": game_status or None,
                })
    return rows


def nfl_com_reports(rows: list[dict], season: int, week: int,
                    now: datetime | None = None,
                    previous: dict[str, dict[str, str]] | None = None,
                    stored: dict[str, dict[str, dict[str, str]]] | None = None,
                    game_dates: dict[str, date] | None = None) -> list[dict]:
    """Date the parsed NFL.com rows and shape them for ``upsert_practice_status``.

    The page carries no report date, so a team's snapshot is dated by the
    clock (``report_day``), corrected with what is already stored:

    ``stored`` maps team -> {date: {name_key: status}} for the week's stored
    NFL.com days. Before the publication cutoff the clock says "yesterday",
    but a team that posts early already shows *today's* report: a snapshot
    that differs from yesterday's stored report is today's and is dated today
    -- it used to overwrite yesterday's row (Brock Bowers' Thursday LP became
    Friday's DNP, 2026-09-26). A snapshot identical to the latest earlier
    stored day is that day's report still on the page (published late) and is
    skipped rather than stored twice under two days; the cost is a genuinely
    identical report on consecutive days being recorded once. ``previous``
    (team -> {name_key: status} of the latest earlier day) is the same check
    for callers without ``stored``.

    ``game_dates`` (team -> this week's game date, from the schedule): a team
    whose page shows another game is not this week's report -- the next
    week's page still shows the last one on Monday and Tuesday, and its final
    report was re-stored under the new week. Without a schedule entry, a game
    already played is skipped for the same reason.
    """
    now = now or datetime.now(UTC)
    et = to_eastern(now)
    before_cutoff = et.hour < REPORT_CUTOFF_HOUR_ET
    by_team: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("practice_status"):
            by_team.setdefault(row["team"], []).append(row)
    out: list[dict] = []
    for team, team_rows in by_team.items():
        gd = team_rows[0].get("game_date")
        game_date = date.fromisoformat(gd) if gd else None
        expected = (game_dates or {}).get(team)
        if game_date and (game_date != expected if expected else game_date < et.date()):
            continue
        snapshot = {norm_name(r["player_name"]): r["practice_status"] for r in team_rows}
        days = (stored or {}).get(team) or {}
        day = report_day(now, game_date)
        if before_cutoff and _published_early(snapshot, day, days, et.date(), game_date):
            day = et.date()
        if day is None:
            continue
        earlier = [d for d in days if d < day.isoformat()]
        prior = days[max(earlier)] if earlier else (previous or {}).get(team)
        if prior and prior == snapshot:
            continue
        for r in team_rows:
            out.append({
                "player_name": r["player_name"], "team": team,
                "position": r.get("position"),
                "date": day.isoformat(), "status": r["practice_status"],
                "description": r.get("practice_description"),
                "injury": r.get("injury"), "game_status": r.get("game_status"),
                "game_date": gd, "season": season, "week": week,
                "estimated": False, "source": SOURCE_NFL_COM,
            })
    return out


def _is_report_day(day: date, game_date: date | None) -> bool:
    """Whether ``day`` can carry a report for a game on ``game_date``."""
    if game_date is None:
        return day.weekday() != 6
    return game_date - timedelta(days=4) <= day <= last_report_day(game_date)


def _published_early(snapshot: dict[str, str], day: date | None,
                     stored_days: dict[str, dict[str, str]], today: date,
                     game_date: date | None) -> bool:
    """Whether a snapshot taken before the cutoff already shows today's report.

    Before the cutoff the clock attributes the page to yesterday. It is
    today's when today can carry a report and either yesterday's report is
    stored and differs from the page, or yesterday cannot carry one at all
    (the week's first report day: the page cannot be an earlier report).
    """
    if not _is_report_day(today, game_date):
        return False
    yesterday = today - timedelta(days=1)
    if day is None:
        return not _is_report_day(yesterday, game_date) and game_date is not None
    if day != yesterday:
        return False  # clamped onto the final report day
    stored = stored_days.get(day.isoformat())
    # Players on the page only: a stored day can hold rows of an earlier,
    # longer version of the same report.
    return bool(stored) and any(stored.get(k) != v for k, v in snapshot.items())


# ---------------------------------------------------------------------------
# ESPN news blurbs
# ---------------------------------------------------------------------------
_DAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday)(?:'s)?\b", re.I)
_FP_RE = re.compile(r"\bfull participant\b|\bfull participation\b|\bpracticed (?:fully|in full)\b", re.I)
_LP_RE = re.compile(
    r"\blimited participant\b|\blimited participation\b|\bwas limited\b|"
    r"\bpracticed (?:on a )?limited(?: basis)?\b|\blimited (?:at|in|during) (?:\w+'s )?practice\b",
    re.I,
)
_DNP_RE = re.compile(
    r"\bnon-participant\b|\bdid not participate\b|\bdidn't participate\b|"
    r"\bdid not practice\b|\bdidn't practice\b|\bsat out (?:\w+'s )?practice\b|"
    r"\bwas held out of (?:\w+'s )?practice\b|\bdid not take part\b|"
    # "remained absent from practice Thursday" (Lamar Jackson, week 5 2026).
    r"\b(?:was|remained|stayed) absent from (?:\w+'s )?practice\b|"
    r"\b(?:was|remained|stayed) out of (?:\w+'s )?practice\b|"
    r"\bmissed (?:\w+'s )?practice\b",
    re.I,
)
# Lines that are never a practice report.
_NOT_A_REPORT_RE = re.compile(r"\bpractice squad\b|\blast week\b", re.I)
# Forward-looking or hedged wording describes no practice that happened. A
# blurb often pairs it with one that did ("was a full participant in
# Wednesday's practice and will be ready for Sunday"), so it only rules out
# participation phrases that are not themselves in the past tense.
_FORWARD_RE = re.compile(
    r"\bwill (?:practice|be|not)\b|\bexpect|\banticipate|\bhopes?\b|\bplans? to\b",
    re.I,
)
_PAST_PREFIX_RE = re.compile(r"\b(?:was|were)\s+(?:a\s+|an\s+)?$", re.I)
_PAST_PHRASES = ("was ", "practiced ", "did not ", "didn't ", "sat out ", "remained ",
                 "stayed ", "missed ")


def _is_past_tense(text: str, m: re.Match) -> bool:
    """Whether a participation phrase reports a practice that already happened."""
    if m.group(0).lower().startswith(_PAST_PHRASES):
        return True
    return bool(_PAST_PREFIX_RE.search(text[max(0, m.start() - 12):m.start()]))


def parse_espn_practice_note(comment: str | None, posted: str | None) -> dict | None:
    """``{status, date, estimated}`` from a news blurb that reports a practice.

    Needs a participation phrase, the word practice (or injury report) and a
    named day; the date is the most recent such weekday on or before the post
    date, in Eastern time. Each phrase belongs to its nearest named day and the
    latest day wins: "full participant in Thursday's practice after he was
    limited Wednesday" is FP on Thursday. Anything else — "will practice
    Wednesday", practice squad moves, two statuses for one day — returns None.
    """
    text = (comment or "").strip()
    if not text or not posted:
        return None
    if not re.search(r"practice|injury report", text, re.I) or _NOT_A_REPORT_RE.search(text):
        return None
    phrases = [(status, m) for status, rx in (("DNP", _DNP_RE), ("LP", _LP_RE), ("FP", _FP_RE))
               for m in rx.finditer(text)]
    if _FORWARD_RE.search(text):
        phrases = [(status, m) for status, m in phrases if _is_past_tense(text, m)]
    days = list(_DAY_RE.finditer(text))
    if not phrases or not days:
        return None
    try:
        posted_at = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except ValueError:
        return None
    posted_day = to_eastern(posted_at).date()

    def _gap(m: re.Match, d: re.Match) -> int:
        return max(d.start() - m.end(), m.start() - d.end(), 0)

    by_date: dict[date, set[str]] = {}
    for status, m in phrases:
        day_m = min(days, key=lambda d: _gap(m, d))
        target = _WEEKDAYS.index(day_m.group(1).lower())
        on = posted_day - timedelta(days=(posted_day.weekday() - target) % 7)
        by_date.setdefault(on, set()).add(status)
    latest = max(by_date)
    if len(by_date[latest]) != 1:
        return None  # two statuses for the same day: ambiguous
    return {
        "status": next(iter(by_date[latest])),
        "date": latest.isoformat(),
        "estimated": "estimated" in text.lower(),
    }


def espn_news_reports(payload: dict, season: int, week: int,
                      since: date | None = None,
                      since_by_team: dict[str, date] | None = None) -> list[dict]:
    """Practice rows from the ESPN league-wide injuries payload.

    ``since``: ignore notes about practices before this date (last week's).
    ``since_by_team``: a tighter per-team floor — the day after the team's
    previous game, when the schedule is known.
    The newest note wins when several describe the same player and day.
    """
    best: dict[tuple[str, str, str], dict] = {}
    for group in (payload or {}).get("injuries") or []:
        team = normalize_team(group.get("displayName"))
        if not team:
            continue
        for item in group.get("injuries") or []:
            parsed = parse_espn_practice_note(item.get("shortComment"), item.get("date"))
            if not parsed:
                continue
            floor = (since_by_team or {}).get(team) or since
            if floor and parsed["date"] < floor.isoformat():
                continue
            name = ((item.get("athlete") or {}).get("displayName") or "").strip()
            if not name:
                continue
            key = (norm_name(name), team, parsed["date"])
            if key in best and (best[key]["posted"] or "") >= (item.get("date") or ""):
                continue
            best[key] = {
                "player_name": name, "team": team,
                "position": (((item.get("athlete") or {}).get("position") or {}).get("abbreviation")),
                "date": parsed["date"], "status": parsed["status"],
                "description": item.get("shortComment"),
                "estimated": parsed["estimated"], "season": season, "week": week,
                "source": SOURCE_ESPN_NEWS, "posted": item.get("date"),
            }
    return [{k: v for k, v in r.items() if k != "posted"} for r in best.values()]


# ---------------------------------------------------------------------------
# Weekly pattern
# ---------------------------------------------------------------------------
def summarize(rows: list[dict] | None) -> dict | None:
    """The week's practice line for one player, or None when nothing was reported.

    ``pattern`` reads like the report ("DNP-LP-FP"); ``trend`` compares the
    latest day with the first (then the one before it on a tie).
    """
    days = sorted((r for r in rows or [] if r.get("status")), key=lambda r: r["date"])
    if not days:
        return None
    statuses = [d["status"] for d in days]
    ranks = [_LADDER.get(s, 1) for s in statuses]
    if len(ranks) < 2:
        trend = "single_report"
    else:
        diff = ranks[-1] - ranks[0] or ranks[-1] - ranks[-2]
        trend = "improving" if diff > 0 else "worsening" if diff < 0 else "steady"
    latest = days[-1]
    sources = sorted({d.get("source") for d in days if d.get("source")})
    return {
        "latest": latest["status"],
        "latest_date": latest["date"],
        "pattern": "-".join(statuses),
        "trend": trend,
        "days": [
            {
                "date": d["date"],
                "day": _DAY_ABBR[date.fromisoformat(d["date"]).weekday()],
                "status": d["status"],
                "estimated": bool(d.get("estimated")),
                "source": d.get("source"),
            }
            for d in days
        ],
        "game_status": next((d.get("game_status") for d in reversed(days) if d.get("game_status")), None),
        "source": sources[0] if len(sources) == 1 else "+".join(sources) if sources else None,
        "updated_at": max((d.get("updated_at") or "") for d in days) or None,
    }


def latest_practice_week(rows: list[dict] | None) -> list[dict]:
    """The rows of the most recent practice week among ``rows``.

    Every team's report week runs Monday (short-week estimate) to Saturday of
    one calendar week, so the latest row's Monday separates it from the
    previous week's days.
    """
    dated = [r for r in rows or [] if r.get("date")]
    if not dated:
        return []
    latest = date.fromisoformat(max(r["date"] for r in dated))
    floor = (latest - timedelta(days=latest.weekday())).isoformat()
    return sorted((r for r in dated if r["date"] >= floor), key=lambda r: r["date"])


def reads_next_week(db, season: int, week: int, now: datetime | None = None) -> bool:
    """Whether a lookup of ``(season, week)`` should also read ``week + 1``.

    Only for the current week: a short-week team's NFL.com report is stored
    under the next week while Sleeper's counter still shows this one on
    Monday and Tuesday. For a past week, reading ``week + 1`` returned the
    following week's report (the latest one wins) as if it were that week's.
    Current means Sleeper's week (the calendar week turning on Wednesday) or
    the schedule's (the first week with a game still to finish).
    """
    from .week_context import infer_from_calendar, infer_from_schedule  # deferred: cycle

    now = now or datetime.now(UTC)
    current = {infer_from_calendar(now)}
    try:
        scheduled = infer_from_schedule(db, now)
    except Exception:
        scheduled = None
    if scheduled:
        current.add(scheduled)
    return (int(season), int(week)) in current


def lookup_practice(db, player_name: str | None, team: str | None,
                    season: int | None = None, week: int | None = None) -> dict | None:
    """``summarize`` of the stored reports for a player known by name and team."""
    team = normalize_team(team)
    if db is None or not player_name or not team or not hasattr(db, "get_practice_reports"):
        return None
    try:
        rows = db.get_practice_reports(player_name, team, season=season, week=week)
    except Exception as e:
        logger.debug(f"practice lookup failed for {player_name}: {e}")
        return None
    return summarize(rows if isinstance(rows, list) else None)


def lookup_practice_with_note(db, player_name: str | None, team: str | None,
                              note: str | None = None, posted: str | None = None,
                              today: date | None = None) -> dict | None:
    """``summarize`` of this practice week's stored reports plus the day a
    current report blurb names (`parse_espn_practice_note`).

    The stored rows lag the news: the ESPN note "remained absent from practice
    Thursday" is the injury table's description hours before a refresh writes
    the day (or NFL.com posts it). A note dated outside the current practice
    week is ignored, and a stored row for the same day wins.
    """
    team = normalize_team(team)
    if not player_name or not team:
        return None
    rows: list[dict] = []
    if db is not None and hasattr(db, "get_practice_reports"):
        try:
            got = db.get_practice_reports(player_name, team)
            rows = list(got) if isinstance(got, list) else []
        except Exception as e:
            logger.debug(f"practice lookup failed for {player_name}: {e}")
    parsed = parse_espn_practice_note(note, posted) if note and posted else None
    if parsed:
        today = today or to_eastern(datetime.now(UTC)).date()
        floor = min(practice_week_start(today), practice_week_start(today, include_monday=True))
        if (floor.isoformat() <= parsed["date"] <= today.isoformat()
                and parsed["date"] not in {r.get("date") for r in rows}):
            rows.append({"date": parsed["date"], "status": parsed["status"],
                         "estimated": parsed["estimated"], "source": SOURCE_ESPN_NEWS})
    return summarize(latest_practice_week(rows))


def practice_fields(summary: dict | None) -> dict:
    """The flat practice fields tools add to a player entry."""
    if not summary:
        return {"practice_status": None, "practice_pattern": None,
                "practice_trend": None, "practice_source": None}
    return {
        "practice_status": summary["latest"],
        "practice_pattern": summary["pattern"],
        "practice_trend": summary["trend"],
        "practice_source": summary["source"],
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
async def fetch_practice_reports(season: int, week: int, db=None, client=None,
                                 now: datetime | None = None) -> list[dict]:
    """This week's real practice rows from NFL.com, plus ESPN news notes.

    ``db`` (optional) supplies the previous stored day per team for the
    unchanged-report check. Network errors degrade to fewer rows, never to
    invented ones.
    """
    from .config import create_http_client, get_http_headers

    now = now or datetime.now(UTC)
    et_today = to_eastern(now).date()
    own = client is None
    client = client or create_http_client()
    reports: list[dict] = []
    try:
        if own:
            await client.__aenter__()
        headers = get_http_headers("nfl_teams")
        # Short-week teams report Mon-Wed, while Sleeper's week counter can
        # still point at the previous week on Monday/Tuesday.
        weeks = [week, week + 1] if et_today.weekday() in (0, 1) else [week]
        for wk in weeks:
            try:
                resp = await client.get(NFL_COM_URL.format(season=season, week=wk),
                                        headers=headers, timeout=20.0)
                if resp.status_code != 200:
                    logger.warning(f"[Practice] NFL.com reg{wk}: HTTP {resp.status_code}")
                    continue
                rows = parse_nfl_com_report(resp.text, season)
                reports.extend(nfl_com_reports(
                    rows, season, wk, now=now, stored=_stored_days(db, season, wk),
                    game_dates=_game_dates(db, season, wk)))
            except Exception as e:
                logger.warning(f"[Practice] NFL.com reg{wk} failed: {e}")
        try:
            resp = await client.get(ESPN_INJURIES_URL, headers=headers, timeout=30.0)
            if resp.status_code == 200:
                reports.extend(espn_news_reports(
                    resp.json(), season, week,
                    since=practice_week_start(et_today, include_monday=True),
                    since_by_team=_after_previous_game(db, season, week),
                ))
        except Exception as e:
            logger.warning(f"[Practice] ESPN notes failed: {e}")
    finally:
        if own:
            await client.__aexit__(None, None, None)
    logger.info(f"[Practice] {len(reports)} real practice rows for {season} week {week}")
    return reports


def _after_previous_game(db, season: int, week: int) -> dict[str, date]:
    """team -> the day after its previous game, from the cached schedule.

    A news note dated before that is about last week's practice.
    """
    if db is None or week <= 1 or not hasattr(db, "get_week_kickoffs"):
        return {}
    from .game_clock import parse_kickoff
    out: dict[str, date] = {}
    try:
        kickoffs = db.get_week_kickoffs(int(season), int(week) - 1) or {}
    except Exception:
        return {}
    for team, kickoff in kickoffs.items():
        ko = parse_kickoff(kickoff)
        code = normalize_team(team)
        if ko and code:
            out[code] = to_eastern(ko).date() + timedelta(days=1)
    return out


def _stored_days(db, season: int, week: int) -> dict[str, dict[str, dict[str, str]]]:
    """team -> {date: {name_key: status}} for the week's stored NFL.com days."""
    if db is None or not hasattr(db, "get_team_practice_snapshots"):
        return {}
    try:
        return db.get_team_practice_snapshots(season, week, source=SOURCE_NFL_COM) or {}
    except Exception:
        return {}


def _game_dates(db, season: int, week: int) -> dict[str, date]:
    """team -> its game date (US Eastern) in ``week``, from the cached schedule."""
    if db is None or not hasattr(db, "get_week_kickoffs"):
        return {}
    from .game_clock import parse_kickoff
    try:
        kickoffs = db.get_week_kickoffs(int(season), int(week)) or {}
    except Exception:
        return {}
    out: dict[str, date] = {}
    for team, kickoff in kickoffs.items():
        ko = parse_kickoff(kickoff)
        code = normalize_team(team)
        if ko and code:
            out[code] = to_eastern(ko).date()
    return out


# In-process throttle for on-demand refreshes from tools.
_last_refresh: dict[tuple[int, int], datetime] = {}
REFRESH_MINUTES = 60


async def refresh_practice_reports(db, season: int | None, week: int | None,
                                   force: bool = False) -> int:
    """Fetch and store the week's reports at most once an hour per process.

    For tools that should not depend on the prefetch loop having run.
    Returns rows written (0 when throttled or nothing new).
    """
    if db is None or not season or not week or not hasattr(db, "upsert_practice_status"):
        return 0
    key = (int(season), int(week))
    now = datetime.now(UTC)
    last = _last_refresh.get(key)
    if not force and last and now - last < timedelta(minutes=REFRESH_MINUTES):
        return 0
    _last_refresh[key] = now
    try:
        rows = await fetch_practice_reports(int(season), int(week), db=db)
    except Exception as e:
        logger.warning(f"[Practice] refresh failed: {e}")
        return 0
    return db.upsert_practice_status(rows) if rows else 0
