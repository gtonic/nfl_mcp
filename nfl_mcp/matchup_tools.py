"""
Matchup analysis tools for fantasy football lineup optimization.

This module provides defense vs position rankings, matchup difficulty analysis,
and game environment factors to help optimize fantasy lineups.
"""

import csv
import logging
from datetime import UTC, datetime, timedelta
from io import StringIO

import httpx

from .config import LONG_TIMEOUT, create_http_client

# nflverse publishes weekly player stats (incl. fantasy points + opponent) as a
# free CSV per season — the reliable source for defense-vs-position.
NFLVERSE_PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv"
)
from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .teams import TEAM_ALIASES, normalize_team

# nflverse abbreviations -> the abbreviations used across this codebase.
# Retained for callers that import it; `normalize_team` is the real mapping.
_NFLVERSE_TEAM_FIX = dict(TEAM_ALIASES)

logger = logging.getLogger(__name__)

# ESPN team ID to abbreviation mapping
ESPN_TEAM_MAP = {
    "1": "ATL", "2": "BUF", "3": "CHI", "4": "CIN", "5": "CLE",
    "6": "DAL", "7": "DEN", "8": "DET", "9": "GB", "10": "TEN",
    "11": "IND", "12": "KC", "13": "LV", "14": "LAR", "15": "MIA",
    "16": "MIN", "17": "NE", "18": "NO", "19": "NYG", "20": "NYJ",
    "21": "PHI", "22": "ARI", "23": "PIT", "24": "LAC", "25": "SF",
    "26": "SEA", "27": "TB", "28": "WSH", "29": "CAR", "30": "JAX",
    "33": "BAL", "34": "HOU"
}

# Reverse mapping
TEAM_TO_ESPN_ID = {v: k for k, v in ESPN_TEAM_MAP.items()}

# Position categories for fantasy
FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]

# Defense ranking tiers
MATCHUP_TIERS = {
    (1, 5): "elite",      # Top 5 - very tough matchup
    (6, 12): "tough",     # 6-12 - above average
    (13, 20): "neutral",  # 13-20 - average
    (21, 27): "favorable", # 21-27 - below average defense
    (28, 32): "smash",    # 28-32 - exploitable matchup
}


# Games of league-average evidence blended into each defense's points allowed.
# Two real games then count for a quarter of the estimate, which is about how
# much they deserve.
SHRINKAGE_GAMES = 6.0
# Below this every tier is reported as neutral. Tiers are derived from the rank,
# so shrinking the underlying points does not move them — the ordering is
# preserved almost exactly. Withholding the tier is the only thing that stops a
# two-game sample from arriving as a confident "smash".
MIN_GAMES_FOR_TIERS = 4
# The continuous matchup factor (`matchup_ratio`) is shrunk toward a prior worth
# this many games, and the prior carries this share of the defense's previous-
# season edge. Chosen by evals/backtest (2023 and 2024 each, out of sample):
# the only setting that lowered MAE and raised Spearman in both seasons, in
# weeks 2-4 and 5+ alike.
MATCHUP_PRIOR_GAMES = 8.0
PRIOR_SEASON_WEIGHT = 0.5


def _get_matchup_tier(rank: int) -> str:
    """Convert numeric rank to tier label."""
    for (low, high), tier in MATCHUP_TIERS.items():
        if low <= rank <= high:
            return tier
    return "neutral"


def _get_tier_color(tier: str) -> str:
    """Get color indicator for tier (for display purposes)."""
    return {
        "elite": "🔴",      # Red - avoid
        "tough": "🟠",      # Orange - caution
        "neutral": "🟡",    # Yellow - standard
        "favorable": "🟢",  # Green - good
        "smash": "💚",      # Bright green - excellent
    }.get(tier, "⚪")


def _init_matchup_db():
    """Initialize database connection for matchup data caching."""
    try:
        from .database import get_shared_db
        return get_shared_db()
    except Exception as e:
        logger.debug(f"Database init failed for matchup tools: {e}")
        return None


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_defense_rankings(weekly: dict, season: int) -> dict[str, list[dict]] | None:
    """Defense-vs-position rankings from ``{(opponent, position, week): [ppr, rec]}``.

    Pure, so the backtest runs exactly what production runs. Besides the rank
    and tier, each entry carries the raw per-game averages (PPR points and
    receptions allowed, and the league's) that `matchup_ratio` needs to price
    the matchup in any league's reception value.
    """
    if not weekly:
        return None
    totals: dict = {}           # (opponent, position) -> [PPR points, receptions]
    weeks_seen: dict = {}       # (opponent, position) -> set of weeks
    for (opp, pos, wk), (pts, rec) in weekly.items():
        cell = totals.setdefault((opp, pos), [0.0, 0.0])
        cell[0] += pts
        cell[1] += rec
        weeks_seen.setdefault((opp, pos), set()).add(wk)

    rankings: dict[str, list[dict]] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        raw = []
        for (opp, p), (total, rec) in totals.items():
            if p != pos:
                continue
            games = max(1, len(weeks_seen.get((opp, pos), {1})))
            raw.append((opp, total / games, rec / games, games))
        if not raw:
            continue

        # Shrink each defense toward the league average. Two games of
        # fantasy points allowed is noise: early in 2026 this put Houston
        # at #3 elite against RBs (12.4/game) and #31 smash against WRs
        # (43.0/game) simultaneously. Same pattern the opportunity model
        # and the playoff variance already use.
        league_mean = sum(v for _, v, _, _ in raw) / len(raw)
        league_rec = sum(r for _, _, r, _ in raw) / len(raw)
        min_games = min(g for _, _, _, g in raw)
        per_team = []
        for team, observed, rec, games in raw:
            shrunk = (games * observed + SHRINKAGE_GAMES * league_mean) / (
                games + SHRINKAGE_GAMES
            )
            per_team.append((team, round(shrunk, 1), observed, rec, games))

        # Fewest points allowed = toughest defense = rank 1 (elite).
        per_team.sort(key=lambda x: x[1])
        # Tiers come from the rank, so shrinking the points alone would
        # leave every tier untouched — the ordering barely moves. Below a
        # usable sample the tier itself has to be withheld, or a two-game
        # artifact keeps arriving as "smash" with full confidence.
        provisional = min_games < MIN_GAMES_FOR_TIERS
        ranked = []
        for rank, (team, shrunk, observed, rec, games) in enumerate(per_team, 1):
            tier = "neutral" if provisional else _get_matchup_tier(rank)
            ranked.append({
                "team": team,
                "rank": rank,
                "points_allowed_avg": shrunk,
                "points_allowed_observed": round(observed, 1),
                "games_sampled": games,
                "matchup_tier": tier,
                "tier_indicator": _get_tier_color(tier),
                "is_provisional": provisional,
                "source": "nflverse",
                "season": season,
                # Unrounded inputs for `matchup_ratio`.
                "ppr_allowed_avg": observed,
                "rec_allowed_avg": rec,
                "league_ppr_avg": league_mean,
                "league_rec_avg": league_rec,
            })
        rankings[pos] = ranked
    return rankings or None


def attach_prior_season(rankings: dict, prior: dict | None) -> dict:
    """Give each entry its defense's full prior-season averages, as the prior
    `matchup_ratio` shrinks toward. Defenses change between seasons, so only
    part of last year's edge is carried over (`PRIOR_SEASON_WEIGHT`)."""
    if not rankings or not prior:
        return rankings
    for pos, rows in rankings.items():
        by_team = {r.get("team"): r for r in prior.get(pos) or []}
        for row in rows:
            old = by_team.get(row.get("team"))
            if not old or "ppr_allowed_avg" not in old:
                continue
            row["prior_ppr_allowed_avg"] = old["ppr_allowed_avg"]
            row["prior_rec_allowed_avg"] = old["rec_allowed_avg"]
            row["prior_league_ppr_avg"] = old["league_ppr_avg"]
            row["prior_league_rec_avg"] = old["league_rec_avg"]
    return rankings


def _allowed_ratio(pts: float, rec: float, league_pts: float, league_rec: float,
                   ppr: float) -> float | None:
    """Points allowed relative to the league average, in `ppr` scoring."""
    mean = league_pts - (1.0 - ppr) * league_rec
    if mean <= 0:
        return None
    return (pts - (1.0 - ppr) * rec) / mean


def matchup_ratio(entry: dict | None, ppr: float = 1.0) -> float | None:
    """How many points this defense allows to the position, relative to average.

    1.10 = gives up 10% more than an average defense. Priced at the league's
    reception value (a defense that bleeds catches matters less in half PPR),
    and shrunk toward a prior worth `MATCHUP_PRIOR_GAMES` games: part of the
    defense's prior-season ratio when one is attached, else average. That is
    what lets the matchup count from week 2 instead of waiting for the
    `MIN_GAMES_FOR_TIERS` the discrete tiers need. None when the entry has no
    raw averages (a fallback or a legacy cached row).
    """
    if not entry or entry.get("is_fallback") or "ppr_allowed_avg" not in entry:
        return None
    games = float(entry.get("games_sampled") or 0)
    observed = _allowed_ratio(entry["ppr_allowed_avg"], entry.get("rec_allowed_avg", 0.0),
                              entry.get("league_ppr_avg", 0.0),
                              entry.get("league_rec_avg", 0.0), ppr)
    if observed is None:
        return None
    prior = 1.0
    if "prior_ppr_allowed_avg" in entry:
        last = _allowed_ratio(entry["prior_ppr_allowed_avg"], entry["prior_rec_allowed_avg"],
                              entry["prior_league_ppr_avg"], entry["prior_league_rec_avg"], ppr)
        if last is not None:
            prior = 1.0 + PRIOR_SEASON_WEIGHT * (last - 1.0)
    return (games * observed + MATCHUP_PRIOR_GAMES * prior) / (games + MATCHUP_PRIOR_GAMES)


class DefenseRankingsAnalyzer:
    """
    Analyzer for NFL defense rankings against fantasy positions.

    Provides matchup difficulty analysis based on points allowed
    to each position by opposing defenses.
    """

    # Default rankings when API fails (based on historical averages)
    # Format: {position: {team: (rank, pts_allowed_avg)}}
    _fallback_rankings = None
    _cache_timestamp = None
    _cache_ttl_hours = 6
    # How long a fallback answer (placeholder or DB copy) is served before
    # nflverse is tried again.
    _fallback_ttl_minutes = 10

    def __init__(self, db=None):
        self.db = db or _init_matchup_db()
        self._rankings_cache = {}

    async def fetch_defense_rankings(self, season: int | None = None) -> dict[str, list[dict]]:
        """
        Fetch defense vs position rankings from ESPN.

        Args:
            season: NFL season year (defaults to current)

        Returns:
            Dict mapping position -> list of team rankings
            Each ranking has: team, rank, points_allowed_avg, matchup_tier
        """
        if season is None:
            from .week_context import current_season_week
            season = (await current_season_week(self.db))["season"]

        # Check cache first. A placeholder answer is kept only briefly so the
        # next call retries nflverse instead of serving neutral ranks for 6h.
        cache_key = f"defense_rankings_{season}"
        if cache_key in self._rankings_cache:
            cached = self._rankings_cache[cache_key]
            ttl = (
                timedelta(minutes=self._fallback_ttl_minutes)
                if cached.get("is_fallback")
                else timedelta(hours=self._cache_ttl_hours)
            )
            if datetime.now(UTC) - cached["timestamp"] < ttl:
                return cached["data"]

        rankings = {}
        is_fallback = False

        # Primary source: nflverse weekly stats -> real fantasy points allowed
        # per game by each defense to each position. (The old ESPN/FantasyPros
        # HTML paths no longer expose this reliably.)
        try:
            async with create_http_client(timeout=LONG_TIMEOUT) as client:
                nfl = await self._fetch_nflverse_rankings(client, season)
                if nfl:
                    nfl = attach_prior_season(nfl, await self._prior_season(client, season - 1))
            if nfl:
                rankings = nfl
            else:
                logger.info(
                    f"No nflverse defense data for {season} yet (preseason?); "
                    "using neutral fallback"
                )
                is_fallback = True
        except Exception as e:
            logger.error(f"Failed to fetch defense rankings: {e}")
            is_fallback = True

        if is_fallback:
            # Last real rankings beat the placeholder table: the database keeps
            # what an earlier fetch persisted, and fallbacks are never stored.
            persisted = self._load_rankings_from_db(season)
            if persisted:
                self._rankings_cache[cache_key] = {
                    "data": persisted,
                    "timestamp": datetime.now(UTC),
                    "is_fallback": True,
                }
                return persisted
            rankings = {pos: self._get_fallback_rankings(pos) for pos in ["QB", "RB", "WR", "TE"]}

        self._rankings_cache[cache_key] = {
            "data": rankings,
            "timestamp": datetime.now(UTC),
            "is_fallback": is_fallback,
        }

        # Persist real rankings only. A stored placeholder was read back by
        # enrichment for a week as a confident neutral rank for every player.
        if self.db and not is_fallback:
            try:
                self._save_rankings_to_db(rankings, season)
            except Exception as e:
                logger.debug(f"Failed to cache rankings to DB: {e}")

        return rankings

    async def _prior_season(self, client, season: int) -> dict | None:
        """Last season's final rankings: the prior of the matchup factor.

        A finished season does not change, so it is fetched once per process;
        a failure is not cached and costs only the prior (neutral instead).
        """
        key = f"prior_season_{season}"
        if key not in self._rankings_cache:
            try:
                prior = await self._fetch_nflverse_rankings(client, season)
            except Exception as e:
                logger.debug(f"prior-season rankings for {season} failed: {e}")
                prior = None
            if not prior:
                return None
            self._rankings_cache[key] = {"data": prior}
        return self._rankings_cache[key]["data"]

    async def _fetch_nflverse_rankings(
        self, client: httpx.AsyncClient, season: int
    ) -> dict[str, list[dict]] | None:
        """Compute defense-vs-position rankings from nflverse weekly stats.

        Aggregates PPR fantasy points allowed per game by each defense to each
        position over the regular season. Returns None when the season's data
        isn't available yet (e.g. preseason).
        """
        url = NFLVERSE_PLAYER_STATS_URL.format(season=season)
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            logger.debug(f"nflverse fetch failed for {season}: {e}")
            return None

        weekly: dict = {}           # (opponent, position, week) -> [PPR points, receptions]
        try:
            for row in csv.DictReader(StringIO(text)):
                if (row.get("season_type") or "").upper() != "REG":
                    continue
                pos = (row.get("position") or row.get("position_group") or "").upper()
                if pos not in ("QB", "RB", "WR", "TE"):
                    continue
                opp = (row.get("opponent_team") or "").upper()
                opp = normalize_team(opp) or opp
                wk = row.get("week")
                if not opp or not wk:
                    continue
                cell = weekly.setdefault((opp, pos, wk), [0.0, 0.0])
                cell[0] += _num(row.get("fantasy_points_ppr"))
                cell[1] += _num(row.get("receptions"))
        except Exception as e:
            logger.debug(f"nflverse parse failed: {e}")
            return None

        return compute_defense_rankings(weekly, season)

    def _normalize_team_name(self, name: str) -> str:
        """Convert team name/city to standard abbreviation."""
        name_lower = name.lower().strip()

        team_mappings = {
            "arizona": "ARI", "cardinals": "ARI",
            "atlanta": "ATL", "falcons": "ATL",
            "baltimore": "BAL", "ravens": "BAL",
            "buffalo": "BUF", "bills": "BUF",
            "carolina": "CAR", "panthers": "CAR",
            "chicago": "CHI", "bears": "CHI",
            "cincinnati": "CIN", "bengals": "CIN",
            "cleveland": "CLE", "browns": "CLE",
            "dallas": "DAL", "cowboys": "DAL",
            "denver": "DEN", "broncos": "DEN",
            "detroit": "DET", "lions": "DET",
            "green bay": "GB", "packers": "GB",
            "houston": "HOU", "texans": "HOU",
            "indianapolis": "IND", "colts": "IND",
            "jacksonville": "JAX", "jaguars": "JAX",
            "kansas city": "KC", "chiefs": "KC",
            "las vegas": "LV", "raiders": "LV",
            "los angeles chargers": "LAC", "chargers": "LAC",
            "los angeles rams": "LAR", "rams": "LAR",
            "miami": "MIA", "dolphins": "MIA",
            "minnesota": "MIN", "vikings": "MIN",
            "new england": "NE", "patriots": "NE",
            "new orleans": "NO", "saints": "NO",
            "new york giants": "NYG", "giants": "NYG",
            "new york jets": "NYJ", "jets": "NYJ",
            "philadelphia": "PHI", "eagles": "PHI",
            "pittsburgh": "PIT", "steelers": "PIT",
            "san francisco": "SF", "49ers": "SF",
            "seattle": "SEA", "seahawks": "SEA",
            "tampa bay": "TB", "buccaneers": "TB",
            "tennessee": "TEN", "titans": "TEN",
            "washington": "WSH", "commanders": "WSH",
        }

        for key, abbr in team_mappings.items():
            if key in name_lower:
                return abbr

        # If already an abbreviation
        if len(name) <= 3:
            return name.upper()

        return name[:3].upper()

    def _get_fallback_rankings(self, position: str) -> list[dict]:
        """Neutral fallback rankings when real data is unavailable (e.g. preseason).

        Every team is marked *neutral* (same middle rank/tier) rather than ranked
        1..32 in alphabetical order — otherwise callers would mistake an arbitrary
        alphabetical ordering for a real ranking. ``is_fallback`` flags each entry.
        """
        teams = list(ESPN_TEAM_MAP.values())
        neutral_rank = 16
        tier = _get_matchup_tier(neutral_rank)
        return [
            {
                "team": team,
                "rank": neutral_rank,
                "points_allowed_avg": 15.0,  # neutral placeholder
                "matchup_tier": tier,
                "tier_indicator": _get_tier_color(tier),
                "is_fallback": True,
            }
            for team in sorted(teams)
        ]

    def _load_rankings_from_db(self, season: int) -> dict[str, list[dict]] | None:
        """Last persisted real rankings for the season (up to a week old), or None."""
        if not self.db or not hasattr(self.db, "get_defense_rankings"):
            return None
        try:
            rankings = self.db.get_defense_rankings(int(season), max_age_hours=24 * 7)
        except Exception as e:
            logger.debug(f"Failed to read rankings from DB: {e}")
            return None
        if not isinstance(rankings, dict) or not rankings:
            return None
        # Legacy placeholder rows give every team the same rank; never serve them.
        if all(len({r.get("rank") for r in rows}) <= 1 for rows in rankings.values()):
            return None
        return rankings

    def _save_rankings_to_db(self, rankings: dict, season: int) -> None:
        """Persist rankings to database for caching."""
        if not self.db:
            return

        try:
            self.db.upsert_defense_rankings(rankings, season)
        except AttributeError:
            # Method might not exist yet in database
            logger.debug("Defense rankings DB method not available")

    def get_matchup_difficulty(
        self,
        position: str,
        opponent_team: str,
        rankings: dict[str, list[dict]] | None = None
    ) -> dict:
        """
        Get matchup difficulty for a player vs opponent.

        Args:
            position: Player's position (QB, RB, WR, TE)
            opponent_team: Opponent team abbreviation
            rankings: Pre-fetched rankings (optional)

        Returns:
            Dict with rank, tier, points_allowed, and recommendation
        """
        position = position.upper()
        # A hand-rolled two-entry map lived here and missed LA/STL/OAK/SD and
        # every full name, each of which silently produced a neutral tier.
        opponent_team = normalize_team(opponent_team) or opponent_team.upper()

        if rankings and position in rankings:
            pos_rankings = rankings[position]
        else:
            # Use cached or fallback
            pos_rankings = self._get_fallback_rankings(position)

        # Find opponent in rankings
        for team_rank in pos_rankings:
            if team_rank["team"] == opponent_team:
                tier = team_rank["matchup_tier"]

                # Honesty: fallback rankings are placeholders (no live data), so
                # do NOT emit a confident smash/avoid call that would mislead.
                if team_rank.get("is_fallback"):
                    return {
                        "position": position,
                        "opponent": opponent_team,
                        "rank": team_rank.get("rank", 16),
                        "rank_display": "n/a",
                        "matchup_tier": "unknown",
                        "tier_indicator": "⚪",
                        "points_allowed_avg": 0,
                        "recommendation": (
                            f"⚠️ No live defense-vs-{position} data available for "
                            f"{opponent_team} — matchup rating unknown (treat as neutral)"
                        ),
                        "is_fallback": True,
                    }

                # Generate recommendation
                if tier == "smash":
                    rec = f"🎯 SMASH SPOT: {opponent_team} allows most points to {position}s"
                elif tier == "favorable":
                    rec = f"✅ Good matchup vs {opponent_team}"
                elif tier == "neutral":
                    rec = f"➡️ Neutral matchup vs {opponent_team}"
                elif tier == "tough":
                    rec = f"⚠️ Tough matchup vs {opponent_team}"
                else:  # elite
                    rec = f"🚫 AVOID: {opponent_team} is elite vs {position}s"

                return {
                    "position": position,
                    "opponent": opponent_team,
                    "rank": team_rank["rank"],
                    "rank_display": f"#{team_rank['rank']}",
                    "matchup_tier": tier,
                    "tier_indicator": team_rank.get("tier_indicator", _get_tier_color(tier)),
                    "points_allowed_avg": team_rank.get("points_allowed_avg", 0),
                    "recommendation": rec,
                    "is_fallback": team_rank.get("is_fallback", False),
                    "is_provisional": bool(team_rank.get("is_provisional", False)),
                }

        # Opponent not found
        return {
            "position": position,
            "opponent": opponent_team,
            "rank": 16,
            "rank_display": "#16",
            "matchup_tier": "neutral",
            "tier_indicator": "🟡",
            "points_allowed_avg": 0,
            "recommendation": f"No data for {opponent_team} vs {position}",
            "is_fallback": True
        }


# Global analyzer instance
_defense_analyzer = None

def get_defense_analyzer() -> DefenseRankingsAnalyzer:
    """Get or create the defense rankings analyzer."""
    global _defense_analyzer
    if _defense_analyzer is None:
        _defense_analyzer = DefenseRankingsAnalyzer()
    return _defense_analyzer


# ---------------------------------------------------------------------------
# Offense strength (points scored per team) — powers DST/K streaming signals.
# Mirror of the defense-vs-position aggregation, keyed by the *scoring* team.
# ---------------------------------------------------------------------------

# season -> (fetched_at, rankings)
_offense_rankings_cache: dict[int, tuple[datetime, dict[str, dict]]] = {}

# The in-progress season's nflverse file grows every week; a finished one never
# changes again.
CURRENT_SEASON_CACHE_TTL = timedelta(hours=3)


def _nfl_season_now(now: datetime | None = None) -> int:
    """The NFL season in progress (January and February belong to last year's)."""
    now = now or datetime.now(UTC)
    return now.year - 1 if now.month < 3 else now.year


def season_cache_fresh(season: int, fetched_at: datetime, now: datetime | None = None) -> bool:
    """Whether a season-keyed nflverse cache entry can still be served.

    Past seasons are final and cached for the life of the process; the current
    (or a future) season expires after ``CURRENT_SEASON_CACHE_TTL`` so a
    long-running server picks up each new week.
    """
    now = now or datetime.now(UTC)
    if season < _nfl_season_now(now):
        return True
    return now - fetched_at < CURRENT_SEASON_CACHE_TTL


# A league-average NFL team's points per game — the prior offense scoring is
# shrunk toward.
LEAGUE_AVG_TEAM_POINTS = 22.0


def _row_points(row: dict) -> float:
    """NFL points one weekly player row put on the board for his team.

    Touchdowns of every kind, two-point conversions, field goals, extra points
    and defensive safeties. The weekly file has no team score column, so this
    is the score rebuilt from the players who produced it.
    """
    def f(col: str) -> float:
        try:
            return float(row.get(col) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    tds = (f("rushing_tds") + f("receiving_tds") + f("special_teams_tds")
           + f("def_tds") + f("fumble_recovery_tds"))
    two_pt = f("rushing_2pt_conversions") + f("receiving_2pt_conversions")
    return 6 * tds + 2 * two_pt + 3 * f("fg_made") + f("pat_made") + 2 * f("def_safeties")


async def fetch_offense_rankings(season: int) -> dict[str, dict]:
    """Rank NFL offenses by PPR points scored per game (nflverse weekly stats).

    Returns ``{team: {"rank": int, "points_scored_avg": float,
    "real_points_avg": float}}`` with rank 1 = highest-scoring (strongest)
    offense. ``points_scored_avg`` is the offense's summed PPR fantasy points
    per game (what the rank is on); ``real_points_avg`` its estimated NFL
    points per game (see `_row_points`), shrunk toward a league-average 22 by
    ``SHRINKAGE_GAMES``; ``games`` the games behind it. Returns ``{}`` when the season's data
    isn't available yet (preseason) so callers can fall back to a prior season.
    """
    cached = _offense_rankings_cache.get(season)
    if cached and season_cache_fresh(season, cached[0]):
        return cached[1]

    url = NFLVERSE_PLAYER_STATS_URL.format(season=season)
    try:
        async with create_http_client(timeout=LONG_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            text = resp.text
    except Exception as e:
        logger.debug(f"nflverse offense fetch failed for {season}: {e}")
        return {}

    weekly: dict = {}       # (team, week) -> summed PPR points
    real: dict = {}         # (team, week) -> estimated NFL points on the board
    weeks_seen: dict = {}   # team -> set of weeks
    try:
        for row in csv.DictReader(StringIO(text)):
            if (row.get("season_type") or "").upper() != "REG":
                continue
            pos = (row.get("position") or row.get("position_group") or "").upper()
            team = (row.get("team") or row.get("recent_team") or "").upper()
            team = normalize_team(team) or team
            wk = row.get("week")
            if not team or not wk:
                continue
            # Every row (kickers and defenders included) adds to the team's
            # real score; only skill rows feed the fantasy-points ranking.
            real[(team, wk)] = real.get((team, wk), 0.0) + _row_points(row)
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            try:
                pts = float(row.get("fantasy_points_ppr") or 0)
            except (TypeError, ValueError):
                pts = 0.0
            weekly[(team, wk)] = weekly.get((team, wk), 0.0) + pts
            weeks_seen.setdefault(team, set()).add(wk)
    except Exception as e:
        logger.debug(f"nflverse offense parse failed: {e}")
        return {}

    if not weekly:
        return {}

    totals: dict = {}
    for (team, _wk), pts in weekly.items():
        totals[team] = totals.get(team, 0.0) + pts

    per_team = [
        (team, round(total / max(1, len(weeks_seen.get(team, {1}))), 1))
        for team, total in totals.items()
    ]
    # Most points scored per game = strongest offense = rank 1.
    per_team.sort(key=lambda x: x[1], reverse=True)
    real_totals: dict = {}
    for (team, _wk), pts in real.items():
        real_totals[team] = real_totals.get(team, 0.0) + pts
    def _real(team: str) -> float:
        # NFL points per game, the scale the K/DEF pricing is built on, shrunk
        # toward a league-average score like the defense rankings are: two
        # games of 38 points are not a 38-point offense. `points_scored_avg`
        # is summed *fantasy* points (~70-120 a game) and cannot stand in.
        games = len(weeks_seen.get(team, ()))
        return round((real_totals.get(team, 0.0) + LEAGUE_AVG_TEAM_POINTS * SHRINKAGE_GAMES)
                     / (games + SHRINKAGE_GAMES), 1)

    rankings = {
        team: {
            "rank": rank,
            "points_scored_avg": ppg,
            "real_points_avg": _real(team),
            "games": len(weeks_seen.get(team, ())),
        }
        for rank, (team, ppg) in enumerate(per_team, 1)
    }
    _offense_rankings_cache[season] = (datetime.now(UTC), rankings)
    return rankings


# MCP Tool Functions

@handle_http_errors(
    default_data={"rankings": {}, "positions": []},
    operation_name="fetching defense rankings"
)
async def get_defense_rankings(
    positions: list[str] | None = None,
    season: int | None = None
) -> dict:
    """
    Get NFL defense rankings against fantasy positions.

    Shows how each NFL defense performs against QBs, RBs, WRs, and TEs,
    helping identify favorable and unfavorable matchups for lineup decisions.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        positions: List of positions to get rankings for (default: all)
                  Valid: "QB", "RB", "WR", "TE"
        season: NFL season year (default: current season)

    Returns:
        Dictionary containing:
        - rankings: Dict mapping position to list of team rankings
        - positions: List of positions included
        - season: Season year
        - tiers_explained: Explanation of matchup tiers

    Example:
        get_defense_rankings(positions=["WR", "RB"])
        -> Shows which defenses are easiest/hardest for WRs and RBs
    """
    analyzer = get_defense_analyzer()
    if season is None:
        from .week_context import current_season_week
        season = (await current_season_week(analyzer.db))["season"]

    # Fetch all rankings
    all_rankings = await analyzer.fetch_defense_rankings(season)

    # Filter to requested positions
    if positions:
        positions = [p.upper() for p in positions]
        filtered = {pos: all_rankings.get(pos, []) for pos in positions if pos in all_rankings}
    else:
        filtered = all_rankings
        positions = list(all_rankings.keys())

    # Surface when the rankings are neutral placeholders (no live data yet),
    # mirroring get_strength_of_schedule so callers don't treat them as real.
    is_fallback = any(
        entry.get("is_fallback")
        for lst in filtered.values()
        for entry in (lst or [])
    )
    if is_fallback:
        message = (
            f"⚠️ No live defense data for {season} "
            "(preseason / not published yet) — rankings are neutral placeholders; "
            "treat matchup grades as low-confidence."
        )
    else:
        message = f"Defense rankings fetched for {len(positions)} positions"

    return create_success_response({
        "rankings": filtered,
        "positions": positions,
        "season": season,
        "total_teams": 32,
        "is_fallback": is_fallback,
        "tiers_explained": {
            "elite": "Ranks 1-5: Tough matchup, consider benching",
            "tough": "Ranks 6-12: Above average defense",
            "neutral": "Ranks 13-20: Average matchup",
            "favorable": "Ranks 21-27: Good matchup opportunity",
            "smash": "Ranks 28-32: Excellent matchup, must start"
        },
        "usage_tip": "Use matchup tier + usage trends + injury status for start/sit decisions",
        "message": message
    })


@handle_http_errors(
    default_data={"matchup": None},
    operation_name="analyzing matchup difficulty"
)
async def get_matchup_difficulty(
    position: str,
    opponent_team: str,
    include_rankings: bool = False
) -> dict:
    """
    Get matchup difficulty for a specific position vs opponent.

    Analyzes how the opponent defense performs against the given position
    and provides a recommendation for lineup decisions.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        position: Fantasy position ("QB", "RB", "WR", "TE")
        opponent_team: Opponent team abbreviation (e.g., "KC", "SF", "DAL")
        include_rankings: Whether to include full position rankings

    Returns:
        Dictionary containing:
        - matchup: Matchup analysis with rank, tier, and recommendation
        - rankings: Full position rankings (if include_rankings=True)

    Example:
        get_matchup_difficulty(position="WR", opponent_team="KC")
        -> Returns KC's defense ranking vs WRs and start/sit recommendation
    """
    analyzer = get_defense_analyzer()

    # Validate position
    position = position.upper()
    if position not in ["QB", "RB", "WR", "TE"]:
        return create_error_response(
            f"Invalid position '{position}'. Must be QB, RB, WR, or TE.",
            error_type=ErrorType.VALIDATION,
            data={"position": position}
        )

    # Fetch rankings
    rankings = await analyzer.fetch_defense_rankings()

    # Get matchup analysis
    matchup = analyzer.get_matchup_difficulty(position, opponent_team, rankings)

    result = {
        "matchup": matchup,
        "message": f"{position} vs {opponent_team}: {matchup['matchup_tier'].upper()} matchup (#{matchup['rank']})"
    }

    if include_rankings:
        result["position_rankings"] = rankings.get(position, [])

    return create_success_response(result)


@handle_http_errors(
    default_data={"analysis": []},
    operation_name="analyzing roster matchups"
)
async def analyze_roster_matchups(
    players: list[dict],
    week: int | None = None
) -> dict:
    """
    Analyze matchup difficulty for multiple players.

    Takes a list of players with their positions and opponents,
    returns matchup analysis for each to help with lineup decisions.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        players: List of player dicts with at least:
                - name: Player name
                - position: QB, RB, WR, TE
                - opponent: Opponent team abbreviation
        week: NFL week number (for display purposes)

    Returns:
        Dictionary containing:
        - analysis: List of matchup analyses per player
        - summary: Aggregated recommendations
        - smash_spots: Players with excellent matchups
        - avoid_spots: Players with tough matchups

    Example:
        analyze_roster_matchups(players=[
            {"name": "Patrick Mahomes", "position": "QB", "opponent": "LV"},
            {"name": "Tyreek Hill", "position": "WR", "opponent": "NE"}
        ])
    """
    analyzer = get_defense_analyzer()

    # Fetch rankings once
    rankings = await analyzer.fetch_defense_rankings()

    analyses = []
    smash_spots = []
    avoid_spots = []

    for player in players:
        name = player.get("name", "Unknown")
        position = player.get("position", "").upper()
        opponent = player.get("opponent", "")

        if not position or position not in ["QB", "RB", "WR", "TE"]:
            analyses.append({
                "player": name,
                "position": position,
                "error": "Invalid or missing position"
            })
            continue

        if not opponent:
            analyses.append({
                "player": name,
                "position": position,
                "error": "Missing opponent"
            })
            continue

        # "BYE" is not a defense: looked up as one it came back a neutral #16.
        from .week_context import is_bye_marker
        if is_bye_marker(opponent):
            analyses.append({
                "player": name, "position": position, "opponent": "BYE",
                "rank": None, "rank_display": None, "matchup_tier": "bye",
                "tier_indicator": None, "on_bye": True,
                "recommendation": "On bye — no game this week, cannot score",
            })
            continue

        matchup = analyzer.get_matchup_difficulty(position, opponent, rankings)

        analysis = {
            "player": name,
            "position": position,
            "opponent": opponent,
            "rank": matchup["rank"],
            "rank_display": matchup["rank_display"],
            "matchup_tier": matchup["matchup_tier"],
            "tier_indicator": matchup["tier_indicator"],
            "recommendation": matchup["recommendation"]
        }
        analyses.append(analysis)

        # Categorize
        if matchup["matchup_tier"] == "smash":
            smash_spots.append(f"{name} ({position}) vs {opponent}")
        elif matchup["matchup_tier"] in ["elite", "tough"]:
            avoid_spots.append(f"{name} ({position}) vs {opponent}")

    # Generate summary
    summary_lines = []
    if smash_spots:
        summary_lines.append(f"🎯 SMASH SPOTS: {', '.join(smash_spots)}")
    if avoid_spots:
        summary_lines.append(f"⚠️ TOUGH MATCHUPS: {', '.join(avoid_spots)}")

    return create_success_response({
        "analysis": analyses,
        "week": week,
        "smash_spots": smash_spots,
        "avoid_spots": avoid_spots,
        "summary": summary_lines,
        "total_analyzed": len(analyses),
        "message": f"Analyzed {len(analyses)} player matchups"
    })
