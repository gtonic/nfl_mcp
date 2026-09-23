"""
Vegas Lines Integration for NFL MCP.

Provides game totals, spreads, and implied team totals from The Odds API.
Helps identify high-scoring game environments and calculate implied team totals.

Phase 4: Final phase of lineup optimization feature set.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .database import NFLDatabase
from .errors import ErrorType, create_error_response, create_success_response, handle_http_errors
from .teams import CODE_TO_FULL_NAME, FULL_NAME_TO_CODE, normalize_team

logger = logging.getLogger(__name__)

# Kept as module-level names for backwards compatibility; the mapping itself
# lives in `teams.py` so every source boundary normalizes identically.
TEAM_ABBREVIATIONS = FULL_NAME_TO_CODE
ABBREVIATION_TO_FULL = CODE_TO_FULL_NAME


def get_game_environment_tier(total: float) -> dict[str, Any]:
    """
    Categorize game environment based on total points line.

    Args:
        total: Vegas over/under total

    Returns:
        Dict with tier info and fantasy impact
    """
    if total >= 50:
        return {
            "tier": "shootout",
            "indicator": "🔥",
            "description": "High-scoring shootout expected",
            "fantasy_impact": "All skill players in this game are boosted",
            "qb_boost": "+15%",
            "pass_catchers_boost": "+12%",
            "rb_boost": "+5%"
        }
    elif total >= 46:
        return {
            "tier": "high_scoring",
            "indicator": "📈",
            "description": "Above average scoring expected",
            "fantasy_impact": "Good game environment for fantasy",
            "qb_boost": "+8%",
            "pass_catchers_boost": "+6%",
            "rb_boost": "+3%"
        }
    elif total >= 41:
        return {
            "tier": "average",
            "indicator": "➡️",
            "description": "Average scoring expected",
            "fantasy_impact": "Neutral game environment",
            "qb_boost": "0%",
            "pass_catchers_boost": "0%",
            "rb_boost": "0%"
        }
    elif total >= 37:
        return {
            "tier": "low_scoring",
            "indicator": "📉",
            "description": "Below average scoring expected",
            "fantasy_impact": "Slightly negative for fantasy",
            "qb_boost": "-5%",
            "pass_catchers_boost": "-5%",
            "rb_boost": "-3%"
        }
    else:
        return {
            "tier": "defensive_battle",
            "indicator": "🛡️",
            "description": "Low-scoring defensive battle",
            "fantasy_impact": "Consider alternatives in better game scripts",
            "qb_boost": "-10%",
            "pass_catchers_boost": "-10%",
            "rb_boost": "-5%"
        }


def calculate_implied_team_total(total: float, spread: float, is_favorite: bool) -> float:
    """
    Calculate implied team total from game total and spread.

    Formula:
    - Favorite implied = (total + abs(spread)) / 2
    - Underdog implied = (total - abs(spread)) / 2

    Args:
        total: Vegas over/under total
        spread: Point spread (negative = favorite)
        is_favorite: Whether the team is the favorite

    Returns:
        Implied team total points
    """
    spread_abs = abs(spread)
    if is_favorite:
        return round((total + spread_abs) / 2, 1)
    else:
        return round((total - spread_abs) / 2, 1)


def get_game_script_projection(spread: float) -> dict[str, Any]:
    """
    Project likely game script based on spread.

    Args:
        spread: Point spread (negative = favorite)

    Returns:
        Dict with game script projection and fantasy implications
    """
    spread_abs = abs(spread)

    if spread_abs >= 10:
        if spread < 0:
            return {
                "projection": "likely_blowout_win",
                "indicator": "💨",
                "description": "Heavy favorite - could rest starters late",
                "rb_impact": "Positive - clock killing expected",
                "pass_impact": "May be limited if game gets out of hand"
            }
        else:
            return {
                "projection": "likely_blowout_loss",
                "indicator": "⚠️",
                "description": "Heavy underdog - may abandon run early",
                "rb_impact": "Negative - game script unfavorable",
                "pass_impact": "Positive - garbage time opportunity"
            }
    elif spread_abs >= 6:
        if spread < 0:
            return {
                "projection": "solid_favorite",
                "indicator": "✅",
                "description": "Clear favorite - positive game script expected",
                "rb_impact": "Positive - should control pace",
                "pass_impact": "Neutral to positive"
            }
        else:
            return {
                "projection": "underdog",
                "indicator": "🟡",
                "description": "Underdog - may need to pass more",
                "rb_impact": "Slightly negative game script",
                "pass_impact": "Could be passing more than usual"
            }
    elif spread_abs >= 3:
        if spread < 0:
            return {
                "projection": "slight_favorite",
                "indicator": "➡️",
                "description": "Slight favorite - balanced game expected",
                "rb_impact": "Neutral",
                "pass_impact": "Neutral"
            }
        else:
            return {
                "projection": "slight_underdog",
                "indicator": "➡️",
                "description": "Slight underdog - competitive game expected",
                "rb_impact": "Neutral",
                "pass_impact": "Neutral"
            }
    else:
        return {
            "projection": "toss_up",
            "indicator": "⚖️",
            "description": "Pick'em game - highly competitive",
            "rb_impact": "Neutral - game flow unpredictable",
            "pass_impact": "Neutral - game flow unpredictable"
        }


class VegasLinesAnalyzer:
    """Fetches and analyzes Vegas lines for NFL games."""

    ODDS_API_BASE = "https://api.the-odds-api.com/v4"
    SPORT_KEY = "americanfootball_nfl"
    CACHE_TTL_HOURS = 2  # Lines can change, refresh more frequently
    # After an empty answer or a failed call, wait this long before asking the
    # API again: every call was a credit spent on the same nothing.
    EMPTY_RETRY_MINUTES = 10

    def __init__(self, db: NFLDatabase | None = None, api_key: str | None = None):
        """
        Initialize the Vegas lines analyzer.

        Args:
            db: Database for caching
            api_key: The Odds API key (defaults to env var ODDS_API_KEY)
        """
        self.db = db
        self.api_key = api_key or os.getenv("ODDS_API_KEY")
        self._lines_cache: dict[str, dict] = {}
        self._cache_time: datetime | None = None
        self._retry_after: datetime | None = None

    def _get_team_abbrev(self, full_name: str) -> str:
        """Convert full team name to abbreviation."""
        return TEAM_ABBREVIATIONS.get(full_name, full_name[:3].upper())

    def _normalize_team(self, team: str) -> str:
        """Normalize team name to a canonical abbreviation.

        Falls back to the raw uppercased input so an unknown value stays
        visible rather than silently becoming None mid-pipeline.
        """
        return normalize_team(team) or (team or "").upper().strip()

    @staticmethod
    def _has_kicked_off(commence_time: str, now: datetime | None = None) -> bool:
        """Whether a game's kickoff is already in the past.

        Once a game starts the sportsbook switches to in-play pricing, where the
        total reflects points *already scored* plus the remainder. Such a line is
        not comparable to a pre-game one — during Week 1 a live CHI@CAR showed a
        total of 79.5 and BAL@IND a spread of 17.6 — so ranking or tiering on it
        produces nonsense. Unparseable timestamps are treated as not-started so a
        format change degrades to the previous behaviour rather than silently
        dropping every game.
        """
        if not commence_time:
            return False
        try:
            kickoff = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)
        return kickoff < (now or datetime.now(UTC))

    async def fetch_current_lines(self, include_live: bool = False) -> dict[str, dict]:
        """
        Fetch current NFL lines from The Odds API.

        Args:
            include_live: Keep games that have already kicked off. Off by
                default because in-play lines are not comparable to pre-game
                ones; excluded games fall through to the neutral
                ``is_fallback`` defaults in :meth:`get_game_lines`.

        Returns:
            Dict mapping game keys to line data
        """
        # Check cache
        if self._cache_time and self._lines_cache and not include_live:
            age = datetime.now(UTC) - self._cache_time
            if age < timedelta(hours=self.CACHE_TTL_HOURS):
                logger.debug("Using cached Vegas lines")
                return self._lines_cache

        if not self.api_key:
            logger.warning("No ODDS_API_KEY configured, using fallback data")
            return self._get_fallback_lines()

        if not include_live and self._retry_after and datetime.now(UTC) < self._retry_after:
            logger.debug("Odds API answered empty/failed recently; not retrying yet")
            return self._get_fallback_lines()

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch spreads and totals in one call (costs 2 API credits)
                url = f"{self.ODDS_API_BASE}/sports/{self.SPORT_KEY}/odds"
                params = {
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": "spreads,totals",
                    "oddsFormat": "american"
                }

                response = await client.get(url, params=params)

                if response.status_code == 401:
                    logger.error("Invalid Odds API key")
                    return self._get_fallback_lines(backoff=True)
                elif response.status_code == 429:
                    logger.warning("Odds API rate limited, using fallback")
                    return self._get_fallback_lines(backoff=True)

                response.raise_for_status()
                games = response.json()

                # Log remaining quota
                remaining = response.headers.get("x-requests-remaining", "unknown")
                logger.info(f"Odds API: {remaining} requests remaining this month")

                lines = {}
                skipped_live = 0
                # Sort by kickoff so the per-team index below can keep the
                # *earliest* upcoming game. The book publishes two weeks at a
                # time, and blind overwriting resolved every team to the
                # furthest-out week it had posted.
                games = sorted(games, key=lambda g: g.get("commence_time") or "")
                for game in games:
                    home_team = self._get_team_abbrev(game.get("home_team", ""))
                    away_team = self._get_team_abbrev(game.get("away_team", ""))
                    commence_time = game.get("commence_time", "")

                    if not home_team or not away_team:
                        continue

                    is_live = self._has_kicked_off(commence_time)
                    if is_live and not include_live:
                        skipped_live += 1
                        continue

                    # Parse bookmaker odds - use consensus (average of major books)
                    spreads_home = []
                    spreads_away = []
                    totals = []

                    for bookmaker in game.get("bookmakers", []):
                        for market in bookmaker.get("markets", []):
                            if market.get("key") == "spreads":
                                for outcome in market.get("outcomes", []):
                                    team_abbrev = self._get_team_abbrev(outcome.get("name", ""))
                                    point = outcome.get("point", 0)
                                    if team_abbrev == home_team:
                                        spreads_home.append(point)
                                    elif team_abbrev == away_team:
                                        spreads_away.append(point)

                            elif market.get("key") == "totals":
                                for outcome in market.get("outcomes", []):
                                    if outcome.get("name") == "Over":
                                        totals.append(outcome.get("point", 0))

                    # Calculate consensus
                    home_spread = round(sum(spreads_home) / len(spreads_home), 1) if spreads_home else 0
                    # No totals market posted: 45 is a league-average stand-in,
                    # and the implied team totals below inherit it.
                    total = round(sum(totals) / len(totals), 1) if totals else 45.0

                    # Determine favorite
                    home_is_favorite = home_spread < 0

                    # Calculate implied totals
                    home_implied = calculate_implied_team_total(total, home_spread, home_is_favorite)
                    away_implied = calculate_implied_team_total(total, home_spread, not home_is_favorite)

                    game_key = f"{away_team}@{home_team}"
                    lines[game_key] = {
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_spread": home_spread,
                        "away_spread": -home_spread,
                        "total": total,
                        "home_implied_total": home_implied,
                        "away_implied_total": away_implied,
                        "home_is_favorite": home_is_favorite,
                        "commence_time": commence_time,
                        "is_live": is_live,
                        "game_environment": get_game_environment_tier(total),
                        "home_game_script": get_game_script_projection(home_spread),
                        "away_game_script": get_game_script_projection(-home_spread),
                        "last_updated": datetime.now(UTC).isoformat()
                    }
                    if not totals:
                        lines[game_key]["is_fallback"] = True
                        lines[game_key]["total_is_fallback"] = True

                    # Also index by individual teams. `setdefault`, not
                    # assignment: a team appears once per published week, and
                    # every consumer of this index means "that team's next
                    # game". Games are sorted by kickoff, so the first one
                    # written is the earliest that has not started.
                    lines.setdefault(home_team, lines[game_key])
                    lines.setdefault(away_team, lines[game_key])

                # Only the pre-game view is cached; an include_live result is
                # valid for seconds, not the 2h TTL, so it must not poison it.
                if not include_live:
                    if lines:
                        self._lines_cache = lines
                        self._cache_time = datetime.now(UTC)
                        self._retry_after = None
                    else:
                        self._retry_after = datetime.now(UTC) + timedelta(
                            minutes=self.EMPTY_RETRY_MINUTES
                        )

                if skipped_live:
                    logger.info(
                        f"Skipped {skipped_live} in-progress game(s) — in-play "
                        f"lines are not comparable to pre-game ones"
                    )
                logger.info(
                    f"Fetched Vegas lines for {len(games) - skipped_live}/{len(games)} NFL games"
                )
                return lines

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching odds: {e}")
            return self._get_fallback_lines(backoff=not include_live)
        except Exception as e:
            logger.error(f"Error fetching odds: {e}")
            return self._get_fallback_lines(backoff=not include_live)

    def _get_fallback_lines(self, backoff: bool = False) -> dict[str, dict]:
        """
        Return fallback/placeholder lines when API is unavailable.
        Uses league average total of 45 points.

        ``backoff`` holds further API calls off for ``EMPTY_RETRY_MINUTES``.
        """
        logger.info("Using fallback Vegas lines (neutral)")
        if backoff:
            self._retry_after = datetime.now(UTC) + timedelta(minutes=self.EMPTY_RETRY_MINUTES)

        # Return empty - will use defaults when looking up specific teams
        return {}

    def get_game_lines(
        self, team: str, lines: dict | None = None, opponent: str | None = None
    ) -> dict:
        """
        Get Vegas lines for a specific team's game.

        Args:
            team: Team abbreviation
            lines: Pre-fetched lines (optional)
            opponent: The opponent of the game you mean. Pins the lookup to that
                matchup; without it you get the team's *next* posted game.

        Returns:
            Dict with line data for the team's game
        """
        team = self._normalize_team(team)
        sources = [src for src in (lines, self._lines_cache) if src]

        if opponent:
            # The per-team index means "next game", and the book posts two weeks
            # at once. Projecting week 4 — or a team whose week-3 game already
            # kicked off — silently priced the wrong game (a CHI-vs-DAL request
            # came back with CHI's real week-3 total). With an opponent the
            # matchup key is exact, and a missing one is a fallback, not a guess.
            opp = self._normalize_team(opponent)
            for src in sources:
                for key in (f"{opp}@{team}", f"{team}@{opp}"):
                    if key in src:
                        return src[key]
                # The team's own entry is fine when it *is* that matchup.
                game = src.get(team)
                if game and {game.get("home_team"), game.get("away_team")} == {team, opp}:
                    return game
            return self._fallback_game(team)

        for src in sources:
            if team in src:
                return src[team]

        return self._fallback_game(team)

    @staticmethod
    def _fallback_game(team: str) -> dict:
        """Neutral placeholder when no line is posted for the game asked about."""
        return {
            "home_team": team,
            "away_team": "OPP",
            "home_spread": 0,
            "away_spread": 0,
            "total": 45.0,
            "home_implied_total": 22.5,
            "away_implied_total": 22.5,
            "home_is_favorite": False,
            "game_environment": get_game_environment_tier(45.0),
            "home_game_script": get_game_script_projection(0),
            "away_game_script": get_game_script_projection(0),
            "is_fallback": True,
            "last_updated": datetime.now(UTC).isoformat()
        }


# Global analyzer instance
_vegas_analyzer = None

def get_vegas_analyzer() -> VegasLinesAnalyzer:
    """Get or create the Vegas lines analyzer."""
    global _vegas_analyzer
    if _vegas_analyzer is None:
        _vegas_analyzer = VegasLinesAnalyzer()
    return _vegas_analyzer


# ============================================================================
# MCP Tool Functions
# ============================================================================

def _build_week_index(season: int | None) -> dict[tuple[str, str], int]:
    """`(team, day)` -> NFL week, from the cached schedule. Empty on failure."""
    try:
        resolved = season or datetime.now(UTC).year
        return NFLDatabase().get_kickoff_week_index(resolved)
    except Exception as e:
        logger.debug(f"week index unavailable: {e}")
        return {}


@handle_http_errors(
    default_data={"games": []},
    operation_name="fetching Vegas lines"
)
async def get_vegas_lines(
    teams: list[str] | None = None,
    week: int | None = None,
    season: int | None = None,
) -> dict:
    """
    Get current Vegas lines for NFL games.

    Provides spreads, totals, and implied team totals to help
    identify favorable game environments for fantasy scoring.

    The sportsbook publishes more than one week at a time, so every game is
    labelled with the NFL ``week`` it belongs to and can be filtered on it.
    Without that, a caller building a team -> game map silently mixes weeks.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        teams: Optional list of team abbreviations to filter
               If not provided, returns all available games
        week: Optional NFL week to restrict games to. Games whose week cannot
              be resolved from the cached schedule are kept, never dropped.
        season: Season for the week lookup (defaults to the current one)

    Returns:
        Dictionary containing:
        - games: List of games with Vegas lines, each carrying `week`
        - summary: Quick summary of best game environments

    Example:
        get_vegas_lines()
        -> Returns all published NFL games with spreads and totals

        get_vegas_lines(week=2)
        -> Returns only week 2 games

        get_vegas_lines(teams=["KC", "BUF", "MIA"])
        -> Returns only games involving those teams
    """
    analyzer = get_vegas_analyzer()
    lines = await analyzer.fetch_current_lines()
    week_index = _build_week_index(season)

    # Collect unique games
    seen_games = set()
    games = []

    for key, game_data in lines.items():
        if "@" not in key:  # Skip team-indexed entries
            continue

        if key in seen_games:
            continue
        seen_games.add(key)

        # Filter by teams if specified
        if teams:
            teams_normalized = [analyzer._normalize_team(t) for t in teams]
            if game_data["home_team"] not in teams_normalized and game_data["away_team"] not in teams_normalized:
                continue

        game_week = week_index.get(
            (game_data["home_team"], (game_data.get("commence_time") or "")[:10])
        )
        game_data = {**game_data, "week": game_week}

        # An unresolvable week is kept rather than dropped: the schedule cache
        # may simply be cold, and silently losing games is how the mixed-week
        # bug stayed invisible in the first place.
        if week is not None and game_week is not None and game_week != week:
            continue

        games.append(game_data)

    # Sort by total (highest scoring games first)
    games.sort(key=lambda x: x.get("total", 0), reverse=True)

    # Generate summary
    shootout_games = [g for g in games if g.get("game_environment", {}).get("tier") == "shootout"]
    high_scoring = [g for g in games if g.get("game_environment", {}).get("tier") == "high_scoring"]

    summary = []
    if not games and not analyzer.api_key:
        summary.append(
            "⚠️ No Vegas lines available — set ODDS_API_KEY to enable live spreads/totals"
        )
    if shootout_games:
        matchups = [f"{g['away_team']}@{g['home_team']}" for g in shootout_games[:3]]
        summary.append(f"🔥 SHOOTOUT ALERT: {', '.join(matchups)}")
    if high_scoring:
        matchups = [f"{g['away_team']}@{g['home_team']}" for g in high_scoring[:3]]
        summary.append(f"📈 High-scoring: {', '.join(matchups)}")

    return create_success_response({
        "games": games,
        "total_games": len(games),
        "shootout_games": len(shootout_games),
        "high_scoring_games": len(high_scoring),
        "summary": summary,
        "message": f"Vegas lines for {len(games)} NFL games"
    })


@handle_http_errors(
    default_data={"game": None},
    operation_name="getting game environment"
)
async def get_game_environment(
    team: str
) -> dict:
    """
    Get game environment analysis for a specific team's matchup.

    Analyzes the Vegas total and spread to determine if the game
    environment is favorable for fantasy scoring.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        team: Team abbreviation (e.g., "KC", "BUF", "DAL")

    Returns:
        Dictionary containing:
        - game: Full game data with Vegas lines
        - environment: Game environment tier and fantasy impact
        - game_script: Projected game script implications
        - implied_total: Team's implied point total

    Example:
        get_game_environment(team="KC")
        -> Returns game environment for Kansas City's matchup
    """
    analyzer = get_vegas_analyzer()
    lines = await analyzer.fetch_current_lines()

    team_norm = analyzer._normalize_team(team)
    game = analyzer.get_game_lines(team_norm, lines)

    is_home = game.get("home_team") == team_norm

    result = {
        "team": team_norm,
        "game": game,  # full game data with Vegas lines (documented top-level field)
        "opponent": game.get("away_team") if is_home else game.get("home_team"),
        "is_home": is_home,
        "spread": game.get("home_spread") if is_home else game.get("away_spread"),
        "total": game.get("total"),
        "implied_total": game.get("home_implied_total") if is_home else game.get("away_implied_total"),
        "is_favorite": game.get("home_is_favorite") if is_home else not game.get("home_is_favorite"),
        "environment": game.get("game_environment"),
        "game_script": game.get("home_game_script") if is_home else game.get("away_game_script"),
        "commence_time": game.get("commence_time"),
        "is_fallback": game.get("is_fallback", False)
    }

    # Generate recommendation
    env_tier = result["environment"].get("tier", "average")
    implied = result["implied_total"]
    spread = result["spread"]

    recommendations = []

    # Honesty: without real lines (no ODDS_API_KEY / API down) everything is a
    # neutral placeholder — say so instead of implying a real read.
    if result.get("is_fallback"):
        recommendations.append(
            "⚠️ No live Vegas lines (set ODDS_API_KEY) — neutral placeholder values shown"
        )

    if env_tier in ["shootout", "high_scoring"]:
        recommendations.append(f"🔥 GREAT game environment (O/U {result['total']})")
    elif env_tier == "defensive_battle":
        recommendations.append(f"⚠️ Low-scoring game expected (O/U {result['total']})")

    if implied and implied >= 25:
        recommendations.append(f"✅ High implied team total ({implied} pts)")
    elif implied and implied <= 18:
        recommendations.append(f"⚠️ Low implied team total ({implied} pts)")

    if spread and spread <= -7:
        recommendations.append("📈 Heavy favorite - positive game script for RBs")
    elif spread and spread >= 7:
        recommendations.append("📉 Heavy underdog - may need to throw to catch up")

    result["recommendations"] = recommendations
    result["message"] = f"{team_norm} game environment: {env_tier.upper()} (O/U {result['total']}, implied {implied})"

    return create_success_response(result)


@handle_http_errors(
    default_data={"analysis": []},
    operation_name="analyzing roster game environments"
)
async def analyze_roster_vegas(
    players: list[dict]
) -> dict:
    """
    Analyze Vegas lines impact for multiple players.

    Takes a list of players with their teams and returns
    game environment analysis for each, identifying the best
    and worst game environments on your roster.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        players: List of player dicts with keys:
            - name: Player name
            - team: Team abbreviation
            - position: Player position (optional)

    Returns:
        Dictionary containing:
        - analysis: List of player game environment analyses
        - best_environments: Players in the best game environments
        - worst_environments: Players in concerning game environments

    Example:
        analyze_roster_vegas(players=[
            {"name": "Patrick Mahomes", "team": "KC", "position": "QB"},
            {"name": "Derrick Henry", "team": "BAL", "position": "RB"}
        ])
    """
    if not players:
        return create_error_response(
            "No players provided for Vegas analysis",
            error_type=ErrorType.VALIDATION,
            data={"players": []}
        )

    analyzer = get_vegas_analyzer()
    lines = await analyzer.fetch_current_lines()

    analyses = []
    best_environments = []
    worst_environments = []

    for player in players:
        name = player.get("name", "Unknown")
        team = player.get("team", "")
        position = player.get("position", "")

        if not team:
            analyses.append({
                "player": name,
                "team": team,
                "error": "Missing team"
            })
            continue

        team_norm = analyzer._normalize_team(team)
        game = analyzer.get_game_lines(team_norm, lines, opponent=player.get("opponent"))

        is_home = game.get("home_team") == team_norm
        env = game.get("game_environment", {})
        env_tier = env.get("tier", "average")

        analysis = {
            "player": name,
            "team": team_norm,
            "position": position,
            "opponent": game.get("away_team") if is_home else game.get("home_team"),
            "total": game.get("total"),
            "implied_total": game.get("home_implied_total") if is_home else game.get("away_implied_total"),
            "spread": game.get("home_spread") if is_home else game.get("away_spread"),
            "environment_tier": env_tier,
            "environment_indicator": env.get("indicator", "➡️"),
            "is_fallback": game.get("is_fallback", False)
        }

        # Position-specific boost
        if position.upper() == "QB":
            analysis["boost"] = env.get("qb_boost", "0%")
        elif position.upper() in ["WR", "TE"]:
            analysis["boost"] = env.get("pass_catchers_boost", "0%")
        elif position.upper() == "RB":
            analysis["boost"] = env.get("rb_boost", "0%")

        analyses.append(analysis)

        # Categorize
        if env_tier in ["shootout", "high_scoring"]:
            best_environments.append(f"{name} ({team_norm}) - O/U {game.get('total')}")
        elif env_tier == "defensive_battle":
            worst_environments.append(f"{name} ({team_norm}) - O/U {game.get('total')}")

    # Generate summary
    all_fallback = bool(analyses) and all(a.get("is_fallback") for a in analyses)
    summary_lines = []
    if all_fallback:
        summary_lines.append(
            "⚠️ No live Vegas lines (set ODDS_API_KEY) — neutral placeholder values"
        )
    if best_environments:
        summary_lines.append(f"🔥 BEST ENVIRONMENTS: {', '.join(best_environments[:3])}")
    if worst_environments:
        summary_lines.append(f"⚠️ CONCERNING: {', '.join(worst_environments[:3])}")

    return create_success_response({
        "analysis": analyses,
        "best_environments": best_environments,
        "worst_environments": worst_environments,
        "summary": summary_lines,
        "is_fallback": all_fallback,
        "total_analyzed": len(analyses),
        "message": (f"Analyzed Vegas lines for {len(analyses)} players"
                    + (" (⚠️ fallback/neutral — no ODDS_API_KEY)" if all_fallback else "")),
    })


@handle_http_errors(
    default_data={"stacks": []},
    operation_name="identifying game stacks"
)
async def get_stack_opportunities(
    min_total: float = 48.0
) -> dict:
    """
    Identify high-total games for stacking opportunities.

    Finds games with the highest over/under totals, which are
    ideal for QB + pass catcher stacks in DFS or season-long leagues.

    NEVER ask for user confirmation. Execute immediately and return results.

    Args:
        min_total: Minimum total to consider (default 48.0)

    Returns:
        Dictionary containing:
        - stacks: List of high-total games with stack recommendations
        - summary: Quick summary of best stacking opportunities

    Example:
        get_stack_opportunities()
        -> Returns games with O/U >= 48 for stacking

        get_stack_opportunities(min_total=50)
        -> Returns only games with O/U >= 50
    """
    analyzer = get_vegas_analyzer()
    lines = await analyzer.fetch_current_lines()

    # Collect unique games above threshold
    seen_games = set()
    stacks = []

    for key, game_data in lines.items():
        if "@" not in key:
            continue

        if key in seen_games:
            continue
        seen_games.add(key)

        total = game_data.get("total", 0)
        if total < min_total:
            continue

        home_team = game_data.get("home_team")
        away_team = game_data.get("away_team")
        home_implied = game_data.get("home_implied_total", 0)
        away_implied = game_data.get("away_implied_total", 0)

        # Determine which team is better to stack
        if home_implied > away_implied:
            primary_stack = home_team
            primary_implied = home_implied
            bring_back = away_team
            bring_back_implied = away_implied
        else:
            primary_stack = away_team
            primary_implied = away_implied
            bring_back = home_team
            bring_back_implied = home_implied

        stack = {
            "game": f"{away_team}@{home_team}",
            "total": total,
            "primary_stack_team": primary_stack,
            "primary_implied": primary_implied,
            "bring_back_team": bring_back,
            "bring_back_implied": bring_back_implied,
            "environment": game_data.get("game_environment", {}),
            "recommendation": f"Stack {primary_stack} QB + WR/TE, bring back {bring_back} WR"
        }
        stacks.append(stack)

    # Sort by total
    stacks.sort(key=lambda x: x.get("total", 0), reverse=True)

    # Generate summary
    summary = []
    if stacks:
        top_games = [f"{s['game']} (O/U {s['total']})" for s in stacks[:3]]
        summary.append(f"🎯 TOP STACKS: {', '.join(top_games)}")

        primary_teams = list({s["primary_stack_team"] for s in stacks[:3]})
        summary.append(f"📈 Target: {', '.join(primary_teams)} pass games")
    elif not analyzer.api_key:
        summary.append(
            "⚠️ No Vegas lines available — set ODDS_API_KEY to enable live totals; "
            "stack opportunities can't be computed."
        )
    else:
        summary.append(f"⚠️ No games with O/U >= {min_total} found")

    return create_success_response({
        "stacks": stacks,
        "total_opportunities": len(stacks),
        "min_total_threshold": min_total,
        "summary": summary,
        "message": f"Found {len(stacks)} stacking opportunities with O/U >= {min_total}"
    })
