"""Sleeper's weekly projections: the primary weekly number, blended with ours.

Sleeper publishes a projected *stat line* per player per week (Rotowire's, at
the time of writing) — pass yards, receptions, FGs by distance, points
allowed. Priced here with the league's own :class:`scoring.ScoringModel`, it is
the larger part of every weekly projection (:func:`blend`):

    projected = BLEND_MODEL_WEIGHT × ours + (1 − BLEND_MODEL_WEIGHT) × Sleeper

From ``evals/backtest/sleeper_blend.py`` (2023-25, weeks 3+, n≈8k player-weeks,
truth priced in the same scoring): ours MAE 5.55, Sleeper 5.40, the 0.25/0.75
blend 5.39 with the best rank correlation of the three. Sleeper wins mostly on
depth-chart knowledge — a benched or demoted player it does not project at all
— and ours adds a little on TEs and on usage the stat line has not caught up
with. Where the two differ by a lot, ``disagreement`` flags it.

A row in Sleeper's list with no projected points is an explicit zero ("Sleeper
does not expect him to play"): 95% of the players our model priced at 5+
points who had such a row did not play that week. No row at all is missing
data, and so is a zero for a team none of whose players are projected yet —
both fall back to our model alone.

Endpoints (undocumented, verified live 2026-09):
  * ``api.sleeper.app/projections/nfl/<season>/<week>?season_type=regular&position[]=...``
    — a list with the player's name, team and opponent; used first so a player
    can be matched by name when the caller has no Sleeper id;
  * ``api.sleeper.app/v1/projections/nfl/regular/<season>/<week>`` — the same
    stat lines keyed by Sleeper id only; the fallback.

Pricing is Sleeper's own rule — each stat times the league's value for that
key — with three corrections: the points-allowed / yards-allowed tier flags
(a single 0/1 tier for the projected mean) are replaced by the chance of each
tier around that mean, as the DEF projection does; a projected 50+ yard FG is
priced at the league's 50-59 value when it has no 50+ key; and a flat
``fgmiss`` applies to every projected miss.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import LONG_TIMEOUT, create_http_client
from .opportunity_tools import norm_name
from .scoring import (
    _POINTS_SD,
    _PTS_ALLOW_TIERS,
    _YARDS_SD,
    _YDS_ALLOW_TIERS,
    ScoringModel,
    _tier_probability,
)
from .teams import normalize_team

logger = logging.getLogger(__name__)

PROJECTIONS_URL = "https://api.sleeper.app/projections/nfl/{season}/{week}"
PROJECTIONS_URL_V1 = "https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}"
PROJECTED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
CACHE_TTL = timedelta(hours=3)

# A gap this large between the two numbers — relative or absolute, whichever
# trips first — is worth a look. The relative test alone would flag a 1.0 vs
# 1.4 kicker; the absolute alone would never flag a kicker at all.
DISAGREE_RELATIVE = 0.25
DISAGREE_ABSOLUTE = 4.0
# Below this neither number says much, and a relative gap between two small
# numbers is noise.
DISAGREE_MIN_POINTS = 3.0
# The relative test also needs a gap this big: 4.9 vs 3.5 is 29% apart and
# still well inside either projection's error.
DISAGREE_MIN_GAP = 2.0

# Share of the weekly projection that is our own model; Sleeper's is the rest.
# One weight for every position and scoring: per position the backtest's best
# weight ranged 0.1 (QB) to 0.3 (TE), each within 0.01 MAE of 0.25, and
# the same 0.25 was best in both real leagues' scoring (see module doc).
BLEND_MODEL_WEIGHT = 0.25
BLEND_WEIGHTS = {"model": BLEND_MODEL_WEIGHT, "sleeper": round(1 - BLEND_MODEL_WEIGHT, 2)}
MODEL_ONLY_WEIGHTS = {"model": 1.0, "sleeper": 0.0}

# (season, week) -> (fetched_at, index); see `_index` for its shape.
_cache: dict[tuple[int, int], tuple[datetime, dict]] = {}


def _index(rows) -> dict:
    """``{"by_id": {id: row}, "by_name": {(name, team): row}, "by_def": {team: row},
    "unprojected": {"by_id", "by_name"}, "teams": {team}}`` from either payload.

    Each row is ``{player_id, name, position, team, opponent, stats}``. Rows
    with projected points go in the main maps; rows Sleeper lists without any
    (a benched QB, an inactive back, a practice-squad player) go in
    ``unprojected`` — an explicit zero, see :func:`points_for`. Only rows with
    a team can be one: the v1 payload carries none, and its empty lines are
    dropped. ``teams`` are the teams with at least one projected player.
    """
    by_id: dict[str, dict] = {}
    by_name: dict[tuple[str, str], dict] = {}
    by_def: dict[str, dict] = {}
    un_id: dict[str, dict] = {}
    un_name: dict[tuple[str, str], dict] = {}
    teams: set[str] = set()
    by_k: dict[str, dict] = {}
    if isinstance(rows, dict):  # the v1 payload: {player_id: stats}
        rows = [{"player_id": pid, "stats": stats} for pid, stats in rows.items()]
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        stats = r.get("stats") or {}
        if not isinstance(stats, dict):
            continue
        player = r.get("player") or {}
        pid = str(r.get("player_id") or "")
        position = (player.get("position") or "").upper() or None
        team = normalize_team(r.get("team") or player.get("team")) or None
        name = " ".join(p for p in (player.get("first_name"), player.get("last_name")) if p)
        row = {"player_id": pid, "name": name or None, "position": position,
               "team": team, "opponent": normalize_team(r.get("opponent")) or None,
               "stats": stats}
        if "pts_ppr" not in stats:
            if team and name:
                if pid:
                    un_id[pid] = row
                un_name[(norm_name(name), team)] = row
            continue
        if team:
            teams.add(team)
            # The team's kicker (the most-projected one), for per-team streaming.
            if position == "K" and float(stats.get("pts_ppr") or 0) >= float(
                    (by_k.get(team) or {}).get("stats", {}).get("pts_ppr") or 0):
                by_k[team] = row
        if pid:
            by_id[pid] = row
        if name and team:
            by_name[(norm_name(name), team)] = row
        # A team defense's id is its (Sleeper-spelled) team code; index it by
        # the canonical code so `WAS`/`WSH` and `JAC`/`JAX` both find it.
        if position == "DEF" or (pid.isalpha() and not position):
            code = team or normalize_team(pid)
            if code:
                by_def[code] = {**row, "position": "DEF", "team": code}
                teams.add(code)
    return {"by_id": by_id, "by_name": by_name, "by_def": by_def,
            "unprojected": {"by_id": un_id, "by_name": un_name}, "teams": teams,
            "by_k": by_k}


async def fetch_week_projections(season: int, week: int) -> dict:
    """Sleeper's projections for one week, indexed; empty index when unavailable."""
    key = (season, week)
    cached = _cache.get(key)
    now = datetime.now(UTC)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    index = await _fetch(season, week)
    if index["by_id"]:
        _cache[key] = (now, index)
    return index


async def _fetch(season: int, week: int) -> dict:
    params = [("season_type", "regular")] + [("position[]", p) for p in PROJECTED_POSITIONS]
    try:
        async with create_http_client(timeout=LONG_TIMEOUT) as client:
            resp = await client.get(PROJECTIONS_URL.format(season=season, week=week), params=params)
            if resp.status_code == 200 and isinstance(resp.json(), list):
                index = _index(resp.json())
                if index["by_id"]:
                    return index
            resp = await client.get(PROJECTIONS_URL_V1.format(season=season, week=week))
            if resp.status_code == 200:
                return _index(resp.json())
    except Exception as e:  # a second opinion must never sink the first
        logger.debug(f"Sleeper projections fetch failed for {season} wk{week}: {e}")
    return {"by_id": {}, "by_name": {}, "by_def": {}}


def price_stats(stats: dict, model: ScoringModel) -> float:
    """A Sleeper projected stat line in the league's scoring (see module doc)."""
    tier_keys = {k for k, _, _ in _PTS_ALLOW_TIERS} | {k for k, _, _ in _YDS_ALLOW_TIERS}
    pts = 0.0
    for key, value in stats.items():
        if key in tier_keys or not isinstance(value, int | float):
            continue
        weight = model.w(key)
        if key == "fgm_50p" and not model.has("fgm_50p"):
            weight = model.w("fgm_50_59")
        pts += weight * float(value)
    if model.w("fgmiss"):
        misses = sum(float(v) for k, v in stats.items()
                     if k.startswith("fgmiss_") and isinstance(v, int | float))
        pts += model.w("fgmiss") * misses
    # Tiers priced as probabilities around the projected mean rather than as
    # the one tier the mean happens to land in.
    if "pts_allow" in stats:
        mean = max(0.0, float(stats["pts_allow"]))
        for key, lo, hi in _PTS_ALLOW_TIERS:
            if model.w(key):
                pts += model.w(key) * _tier_probability(lo, hi, mean, _POINTS_SD)
    if "yds_allow" in stats:
        mean = max(0.0, float(stats["yds_allow"]))
        for key, lo, hi in _YDS_ALLOW_TIERS:
            if model.w(key):
                pts += model.w(key) * _tier_probability(lo, hi, mean, _YARDS_SD)
    return round(pts, 1)


def lookup(index: dict, *, player_id: str | None = None, name: str | None = None,
           team: str | None = None, position: str | None = None) -> dict | None:
    """The player's Sleeper projection row: by id, else by name + team.

    A team defense also matches on its abbreviation alone (Sleeper's DEF id
    *is* the team code).
    """
    team = normalize_team(team) or (team or "").upper() or None
    if (position or "").upper() in ("DEF", "DST"):
        code = team or normalize_team(player_id) or normalize_team(name)
        return index.get("by_def", {}).get(code) if code else None
    if player_id and (row := index["by_id"].get(str(player_id))):
        # Only trusted when the name agrees: callers pass ids from more than
        # one id space, and an ESPN id can be some other player's Sleeper id.
        if not (name and row.get("name")) or norm_name(name) == norm_name(row["name"]):
            return row
    if name and team:
        return index["by_name"].get((norm_name(name), team))
    return None


def _unprojected_row(index: dict, *, player_id: str | None, name: str | None,
                     team: str | None) -> dict | None:
    """The player's row among those Sleeper lists without points (name-checked)."""
    un = index.get("unprojected") or {}
    team = normalize_team(team) or (team or "").upper() or None
    if player_id and (row := (un.get("by_id") or {}).get(str(player_id))):
        if not (name and row.get("name")) or norm_name(name) == norm_name(row["name"]):
            return row
    if name and team:
        return (un.get("by_name") or {}).get((norm_name(name), team))
    return None


def points_for(index: dict, model: ScoringModel, *, player_id: str | None = None,
               name: str | None = None, team: str | None = None,
               position: str | None = None) -> tuple[float | None, str]:
    """``(points, status)``: Sleeper's number for a player in this scoring.

    status is ``projected`` (a priced stat line), ``not_projected`` (listed
    without points: an explicit 0.0 — a benched starter, an inactive back) or
    ``missing`` (None). A listed-but-unprojected player only counts as zero
    when Sleeper projects somebody else on his team this week; otherwise the
    team simply has not been published yet, and that is missing data.
    """
    row = lookup(index, player_id=player_id, name=name, team=team, position=position)
    if row:
        return price_stats(row["stats"], model), "projected"
    if (position or "").upper() in ("DEF", "DST"):
        return None, "missing"
    row = _unprojected_row(index, player_id=player_id, name=name, team=team)
    if row and row.get("team") in (index.get("teams") or set()):
        return 0.0, "not_projected"
    return None, "missing"


def weights_for(status: str | None) -> dict[str, float]:
    """``{"model", "sleeper"}`` shares for a :func:`points_for` status."""
    if status == "projected":
        return dict(BLEND_WEIGHTS)
    if status == "not_projected":
        return {"model": 0.0, "sleeper": 1.0}
    return dict(MODEL_ONLY_WEIGHTS)


def blend(model_points: float, sleeper_points: float, availability_kind: str = "healthy",
          injury_mult: float = 1.0, status: str = "projected") -> float:
    """The weekly projection from ours and Sleeper's (see module doc).

    `model_points` already carry our availability multiplier, Sleeper's its own
    read of the injury. So a questionable tag discounts only our quarter of the
    number instead of being charged twice. Out is zero whatever Sleeper says.
    Doubtful is capped at our doubtful share of the healthier of the two
    readings: Sleeper often still projects a doubtful player in full.

    A player Sleeper lists without points (`status` ``not_projected``) is
    zero outright rather than a quarter of ours: in the backtest those players
    scored 0.05 points a week on average (723 player-weeks our model priced at
    11.7), and zeroing them cut the all-players MAE 5.17 -> 4.94.
    """
    if availability_kind == "out" or injury_mult == 0.0 or status == "not_projected":
        return 0.0
    mixed = BLEND_MODEL_WEIGHT * model_points + (1 - BLEND_MODEL_WEIGHT) * sleeper_points
    if availability_kind == "doubtful" and 0.0 < injury_mult < 1.0:
        healthy = model_points / injury_mult
        mixed = min(mixed, injury_mult * max(healthy, sleeper_points))
    return round(max(0.0, mixed), 1)


def second_opinion(ours: float | None, theirs: float | None, ruled_out: bool = False) -> dict:
    """``{sleeper_projection, consensus, disagreement, gap}`` for two numbers.

    `ours` is our model's number (``model_projection``), not the blend.
    ``consensus`` is the blend of the two (:data:`BLEND_MODEL_WEIGHT`), before
    any injury cap. `ruled_out`: our zero comes from a status that rules him
    out. The disagreement still stands — it says "check his status" — but the
    consensus is not part of a player who will not take the field.
    """
    if theirs is None:
        return {"sleeper_projection": None, "consensus": None,
                "disagreement": False, "gap": None}
    if ours is None:
        return {"sleeper_projection": theirs, "consensus": None,
                "disagreement": False, "gap": None}
    gap = round(ours - theirs, 1)
    base = max(abs(ours), abs(theirs))
    disagree = base >= DISAGREE_MIN_POINTS and (
        abs(gap) > DISAGREE_ABSOLUTE
        or (abs(gap) > DISAGREE_RELATIVE * base and abs(gap) >= DISAGREE_MIN_GAP))
    out = {
        "sleeper_projection": theirs,
        "consensus": 0.0 if ruled_out else round(
            BLEND_MODEL_WEIGHT * ours + (1 - BLEND_MODEL_WEIGHT) * theirs, 1),
        "disagreement": disagree,
        # Positive: we are higher than Sleeper.
        "gap": gap,
    }
    if ruled_out and disagree:
        out["disagreement_note"] = "ruled out on our injury data; Sleeper still projects him"
    return out


async def attach(result: dict, inputs: list[dict], season: int | None, week: int | None,
                 scoring) -> dict:
    """The projection tools' hook: annotate ``result["projections"]`` in place
    and set ``result["sleeper_second_opinion"]``. Never raises."""
    from .scoring import resolve_scoring
    try:
        summary = await annotate(result.get("projections") or [], inputs, season, week,
                                 resolve_scoring(scoring))
    except Exception as e:  # a second opinion must never sink the first
        logger.warning(f"Sleeper second opinion failed: {e}")
        summary = {"active": False, "source": None, "disagreements": []}
    result["sleeper_second_opinion"] = summary
    return result


async def annotate(projections: list[dict], inputs: list[dict], season: int | None,
                   week: int | None, model: ScoringModel) -> dict:
    """Add ``sleeper_projection`` / ``consensus`` / ``disagreement`` / ``gap``
    to each projection (in place) and return a summary block.

    `inputs` are the players as passed to the projection, in the same order —
    they carry the Sleeper `player_id` the projection output does not. A
    player on bye keeps a zero and is never a disagreement.
    """
    empty = {"active": False, "source": None, "disagreements": []}
    if not projections or not season or not week:
        return empty
    index = await fetch_week_projections(season, week)
    if not index["by_id"]:
        return {**empty, "note": "Sleeper projections unavailable — no second opinion."}
    disagreements = []
    for proj, given in zip(projections, inputs, strict=False):
        if proj.get("on_bye"):
            proj.update(second_opinion(0.0, 0.0))
            continue
        theirs, _status = points_for(index, model, player_id=(given or {}).get("player_id"),
                                     name=proj.get("player"), team=proj.get("team"),
                                     position=proj.get("position"))
        ruled_out = (proj.get("breakdown") or {}).get("injury_mult") == 0.0
        # Held against our model's own number: the blended one is mostly
        # Sleeper's already, and a gap to it would say little.
        ours = proj.get("model_projection", proj.get("projected_points"))
        proj.update(second_opinion(ours, theirs, ruled_out))
        if proj["disagreement"]:
            disagreements.append({
                "player": proj.get("player"), "position": proj.get("position"),
                "ours": ours, "sleeper": theirs, "gap": proj["gap"],
            })
    disagreements.sort(key=lambda d: abs(d["gap"]), reverse=True)
    return {
        "active": True,
        "source": "Sleeper weekly projections (Rotowire stat lines), priced in this league's scoring",
        "matched": sum(1 for p in projections if p.get("sleeper_projection") is not None),
        "disagreements": disagreements,
        "rule": (f"disagreement = gap > {DISAGREE_ABSOLUTE:g} pts or > "
                 f"{DISAGREE_RELATIVE:.0%} of the larger number and >= "
                 f"{DISAGREE_MIN_GAP:g} pts (ignored below {DISAGREE_MIN_POINTS:g} pts)"),
        "note": (f"projected_points is the Sleeper-first blend ({BLEND_MODEL_WEIGHT:g} ours + "
                 f"{1 - BLEND_MODEL_WEIGHT:g} Sleeper, evals/backtest/sleeper_blend.py); "
                 "disagreement compares our model (model_projection) with Sleeper."),
    }
