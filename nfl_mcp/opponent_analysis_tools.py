"""
Opponent analysis tools for the NFL MCP Server.

This module provides fantasy football opponent analysis functionality including
roster weakness identification, matchup vulnerability assessment, and strategic
exploitation recommendations.
"""

import logging
from collections import defaultdict

from .errors import ErrorType, create_error_response, create_success_response
from .lineup_slots import slot_accepts, starting_slots
from .sleeper_tools import active_enriched, get_league_users, get_matchups, get_rosters

logger = logging.getLogger(__name__)

# Kickers and defenses are one-a-week units: nobody carries a second, and
# "snap share" does not apply to either. Depth and snap penalties made every
# K and DEF group "critical" regardless of who it was.
_UNIT_POSITIONS = frozenset({"K", "DEF", "DST"})
_ASSESSED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def league_positions(roster_positions) -> list[str]:
    """The assessed positions a league can actually start.

    A position counts when any starting slot takes it (a FLEX makes no new
    position; a league with no K slot has no kicker to be weak at). Without
    roster positions every position is assessed.
    """
    slots = starting_slots(roster_positions)
    if not slots:
        return list(_ASSESSED_POSITIONS)
    return [pos for pos in _ASSESSED_POSITIONS
            if any(slot_accepts(slot, pos) for slot in slots)]


class OpponentAnalyzer:
    """Analyzer for identifying and exploiting opponent roster weaknesses."""

    def __init__(self):
        # Position importance weights for fantasy
        self.position_weights = {
            "QB": 1.0,
            "RB": 1.3,
            "WR": 1.1,
            "TE": 1.2,
            "K": 0.5,
            "DEF": 0.7
        }

        # Thresholds for weakness detection
        self.weakness_thresholds = {
            "snap_pct_low": 40.0,
            "depth_count_low": 2,
            "injury_risk_high": ["DNP", "LP"]
        }

    def _assess_position_strength(
        self,
        players_at_position: list[dict],
        position: str
    ) -> dict:
        """
        Assess the strength of a specific position group.

        Args:
            players_at_position: List of players at the position
            position: Position being assessed (QB, RB, WR, TE, etc.)

        Returns:
            Dict with strength assessment including score, depth, and concerns
        """
        if not players_at_position:
            return {
                "strength_score": 0,
                "depth_count": 0,
                "average_snap_pct": 0,
                "injury_concerns": 0,
                "weakness_level": "critical",
                "concerns": ["No players at position"]
            }

        # Calculate metrics
        depth_count = len(players_at_position)
        snap_pcts = [p.get("snap_pct", 0) for p in players_at_position if p.get("snap_pct")]
        avg_snap_pct = sum(snap_pcts) / len(snap_pcts) if snap_pcts else 0

        # Count injury concerns
        injury_concerns = 0
        injured_players = []
        for player in players_at_position:
            status = player.get("practice_status")
            if status in self.weakness_thresholds["injury_risk_high"]:
                injury_concerns += 1
                injured_players.append(player.get("full_name", "Unknown"))

        # Count usage concerns
        usage_concerns = 0
        declining_players = []
        for player in players_at_position:
            trend = player.get("usage_trend_overall")
            if trend == "down":
                usage_concerns += 1
                declining_players.append(player.get("full_name", "Unknown"))

        # Calculate strength score (0-100)
        base_score = 50.0
        is_unit = (position or "").upper() in _UNIT_POSITIONS

        # Depth contribution (more depth = stronger). Not for K/DEF: one is
        # the whole position.
        if is_unit:
            pass
        elif depth_count >= 4:
            base_score += 20
        elif depth_count >= 3:
            base_score += 10
        elif depth_count <= 1:
            base_score -= 20

        # Snap percentage contribution (K/DEF have no offensive snap share)
        if is_unit:
            pass
        elif avg_snap_pct >= 70:
            base_score += 15
        elif avg_snap_pct >= 50:
            base_score += 5
        elif avg_snap_pct < 40:
            base_score -= 15

        # Injury penalty
        base_score -= injury_concerns * 10

        # Usage trend penalty
        base_score -= usage_concerns * 5

        # Clamp score
        strength_score = max(0, min(100, base_score))

        # Determine weakness level
        if strength_score >= 70:
            weakness_level = "strong"
        elif strength_score >= 50:
            weakness_level = "moderate"
        elif strength_score >= 30:
            weakness_level = "weak"
        else:
            weakness_level = "critical"

        # Compile concerns
        concerns = []
        if not is_unit and depth_count < self.weakness_thresholds["depth_count_low"]:
            concerns.append(f"Shallow depth ({depth_count} player{'s' if depth_count != 1 else ''})")
        if not is_unit and avg_snap_pct < self.weakness_thresholds["snap_pct_low"]:
            concerns.append(f"Low snap share (avg {avg_snap_pct:.1f}%)")
        if injured_players:
            concerns.append(f"Injury concerns: {', '.join(injured_players)}")
        if declining_players:
            concerns.append(f"Declining usage: {', '.join(declining_players)}")

        return {
            "strength_score": round(strength_score, 1),
            "depth_count": depth_count,
            "average_snap_pct": round(avg_snap_pct, 1),
            "injury_concerns": injury_concerns,
            "usage_concerns": usage_concerns,
            "weakness_level": weakness_level,
            "concerns": concerns
        }

    def _identify_starter_weaknesses(
        self,
        starters: list[dict]
    ) -> list[dict]:
        """
        Identify specific weaknesses in starting lineup.

        Args:
            starters: List of starting players

        Returns:
            List of weakness dictionaries with player info and severity
        """
        weaknesses = []

        for starter in starters:
            player_weaknesses = []
            severity = "low"

            # Check practice status
            practice_status = starter.get("practice_status")
            if practice_status == "DNP":
                player_weaknesses.append("Did not practice (DNP)")
                severity = "high"
            elif practice_status == "LP":
                player_weaknesses.append("Limited practice (LP)")
                severity = "moderate" if severity == "low" else severity

            # Check usage trend
            usage_trend = starter.get("usage_trend_overall")
            if usage_trend == "down":
                player_weaknesses.append("Declining usage trend")
                severity = "moderate" if severity == "low" else severity

            # Check snap percentage
            snap_pct = starter.get("snap_pct", 0)
            if snap_pct > 0 and snap_pct < 50:
                player_weaknesses.append(f"Low snap share ({snap_pct:.1f}%)")
                severity = "moderate" if severity == "low" else severity

            if player_weaknesses:
                weaknesses.append({
                    "player_id": starter.get("player_id"),
                    "player_name": starter.get("full_name", "Unknown"),
                    "position": starter.get("position", "Unknown"),
                    "weaknesses": player_weaknesses,
                    "severity": severity
                })

        return weaknesses

    def _generate_exploitation_strategies(
        self,
        position_assessments: dict[str, dict],
        starter_weaknesses: list[dict]
    ) -> list[dict]:
        """
        Generate strategic recommendations for exploiting opponent weaknesses.

        Args:
            position_assessments: Assessment by position
            starter_weaknesses: Identified starter weaknesses

        Returns:
            List of strategic recommendations with priority
        """
        strategies = []

        # Identify weakest positions
        weak_positions = []
        for position, assessment in position_assessments.items():
            if assessment["weakness_level"] in ["weak", "critical"]:
                weak_positions.append({
                    "position": position,
                    "score": assessment["strength_score"],
                    "concerns": assessment["concerns"]
                })

        # Sort by weakness (lowest score = weakest)
        weak_positions.sort(key=lambda x: x["score"])

        # Generate position-based strategies
        for weak_pos in weak_positions[:3]:  # Top 3 weakest
            position = weak_pos["position"]
            score = weak_pos["score"]

            priority = "critical" if score < 30 else "high" if score < 50 else "moderate"

            strategy = {
                "category": "position_weakness",
                "position": position,
                "priority": priority,
                "recommendation": f"Target {position} position - opponent has critical weakness",
                "details": weak_pos["concerns"],
                "action_items": []
            }

            if position in ["RB", "WR", "TE"]:
                strategy["action_items"].append(
                    f"Start your strongest {position} against this opponent"
                )
                strategy["action_items"].append(
                    f"Consider flex spot for additional {position}"
                )
            elif position == "QB":
                strategy["action_items"].append(
                    "Opponent QB weakness may lead to fewer points scored"
                )
                strategy["action_items"].append(
                    "Their defense may be on field longer"
                )

            strategies.append(strategy)

        # Generate starter-specific strategies
        high_severity_starters = [
            w for w in starter_weaknesses
            if w["severity"] in ["high", "moderate"]
        ]

        if high_severity_starters:
            for weakness in high_severity_starters[:2]:  # Top 2
                strategy = {
                    "category": "starter_vulnerability",
                    "position": weakness["position"],
                    "priority": weakness["severity"],
                    "recommendation": f"Exploit {weakness['player_name']} vulnerability",
                    "details": weakness["weaknesses"],
                    "action_items": [
                        f"Target players who will face {weakness['player_name']}",
                        "Monitor injury reports for this player"
                    ]
                }
                strategies.append(strategy)

        return strategies

    def analyze_opponent_roster(
        self,
        opponent_roster: dict,
        starters: list[dict] | None = None,
        roster_positions: list[str] | None = None,
    ) -> dict:
        """
        Perform comprehensive analysis of opponent roster.

        Args:
            opponent_roster: Opponent's roster data with enriched players
            starters: This week's starters (from the week's matchup). The
                roster's own ``starters_enriched`` is the lineup as last saved,
                which is stale until the manager sets this week's; it is only
                the fallback.
            roster_positions: the league's Sleeper ``roster_positions``. Only
                positions it has a starting slot for are assessed (default: all).

        Returns:
            Dict with complete opponent analysis
        """
        # Get players and starters
        # IR/taxi players cannot start, so they must not make an opponent
        # look deep at a position they are actually thin at.
        all_players = active_enriched(opponent_roster)
        if starters is None:
            starters = opponent_roster.get("starters_enriched", [])

        # An empty (undrafted / pre-draft) roster isn't "100% vulnerable" —
        # every position would score 0 and invert to a max vulnerability. Flag
        # it as no-data instead of fabricating "critical" weaknesses.
        if not all_players and not starters:
            return {
                "vulnerability_score": None,
                "vulnerability_level": "unknown",
                "no_data": True,
                "position_assessments": {},
                "starter_weaknesses": [],
                "exploitation_strategies": [],
                "roster_id": opponent_roster.get("roster_id"),
                "owner_id": opponent_roster.get("owner_id"),
                "message": "Opponent roster is empty (not drafted yet) — nothing to analyze.",
            }

        # Group players by position
        players_by_position = defaultdict(list)
        for player in all_players:
            pos = player.get("position", "")
            if pos:
                players_by_position[pos].append(player)

        # Assess each position
        position_assessments = {}
        positions = league_positions(roster_positions)
        for position in positions:
            position_assessments[position] = self._assess_position_strength(
                players_by_position[position],
                position
            )

        # Identify starter weaknesses
        starter_weaknesses = self._identify_starter_weaknesses(starters)

        # Generate exploitation strategies
        strategies = self._generate_exploitation_strategies(
            position_assessments,
            starter_weaknesses
        )

        # Calculate overall vulnerability score
        position_scores = [
            assessment["strength_score"] * self.position_weights.get(pos, 1.0)
            for pos, assessment in position_assessments.items()
        ]
        weighted_avg = sum(position_scores) / sum(
            self.position_weights.get(pos, 1.0) for pos in position_assessments)

        # Invert to get vulnerability (lower strength = higher vulnerability)
        vulnerability_score = 100 - weighted_avg

        return {
            "vulnerability_score": round(vulnerability_score, 1),
            "vulnerability_level": (
                "high" if vulnerability_score >= 60 else
                "moderate" if vulnerability_score >= 40 else
                "low"
            ),
            "position_assessments": position_assessments,
            "starter_weaknesses": starter_weaknesses,
            "exploitation_strategies": strategies,
            "roster_id": opponent_roster.get("roster_id"),
            "owner_id": opponent_roster.get("owner_id"),
            "positions_assessed": positions,
        }


async def _league_roster_positions(league_id: str) -> list[str] | None:
    """The league's ``roster_positions`` (None when the league cannot be read)."""
    from .sleeper_tools import get_league
    try:
        league = ((await get_league(league_id)) or {}).get("league") or {}
    except Exception as e:
        logger.debug(f"league {league_id} unavailable for roster positions: {e}")
        return None
    return league.get("roster_positions") or None


async def _season_week(db=None) -> dict:
    """``{season, week}`` now (a seam for tests)."""
    from .week_context import current_season_week
    return await current_season_week(db)


def _matchup_starters(matchup: dict, roster: dict) -> list[dict]:
    """This week's starters from the matchup, enriched where we can.

    The matchup carries the lineup as set for *this* week; Sleeper's roster
    `starters` is whatever was saved last, which is last week's until the
    manager touches it. "0" is Sleeper's empty-slot marker.
    """
    ids = [str(p) for p in (matchup.get("starters") or []) if p and str(p) != "0"]
    known: dict[str, dict] = {}
    for p in (roster.get("players_enriched") or []) + (matchup.get("starters_enriched") or []):
        if p.get("player_id") is not None:
            known[str(p["player_id"])] = p
    return [known.get(pid) or {"player_id": pid} for pid in ids]


async def _project_starters(starters: list[dict], league_id: str, season: int | None,
                            week: int, db) -> dict | None:
    """Projected points of this week's starters in the league's own scoring.

    Sleeper's ``custom_points`` is a commissioner's manual override (null in
    almost every league), not a projection.
    """
    from . import projections
    from .teams import normalize_team

    ids = [str(p.get("player_id")) for p in starters if p.get("player_id")]
    if not ids:
        return None
    rows = db.get_athletes_by_ids(ids) if db is not None else {}
    inputs, unprojected = [], []
    for p in starters:
        pid = str(p.get("player_id"))
        row = rows.get(pid) or {}
        position = (row.get("position") or p.get("position") or "").upper()
        team = normalize_team(row.get("team_id")) or (
            normalize_team(pid) if position in ("DEF", "DST") else None)
        name = team if position in ("DEF", "DST") else (row.get("full_name") or p.get("full_name"))
        if not (team and name and position):
            unprojected.append(p.get("full_name") or pid)
            continue
        inputs.append({"name": name, "position": position, "team": team, "player_id": pid})
    if not inputs:
        return None
    res = await projections.project_players(inputs, season=season, week=week, db=db,
                                            league_id=league_id)
    rows_out = [
        {"player": r.get("player"), "position": r.get("position"),
         "projected_points": r.get("projected_points"), "on_bye": r.get("on_bye")}
        for r in (res or {}).get("projections") or []
    ]
    if not rows_out:
        return None
    return {
        "projected_points": round(sum(float(r["projected_points"] or 0) for r in rows_out), 1),
        "starters": rows_out,
        "unprojected": unprojected,
    }


async def analyze_opponent(
    league_id: str,
    opponent_roster_id: int,
    current_week: int | None = None,
    db=None,
) -> dict:
    """
    Analyze an opponent's roster to identify weaknesses and exploitation opportunities.

    This tool provides comprehensive analysis of an opponent's fantasy roster including:
    - Position-by-position strength assessment
    - Starter vulnerability identification
    - Depth chart weakness analysis
    - Injury and usage trend concerns
    - Strategic recommendations for exploitation

    Args:
        league_id: The unique identifier for the fantasy league
        opponent_roster_id: Roster ID of the opponent to analyze
        current_week: Optional current NFL week for matchup context

    Returns:
        A dictionary containing:
        - vulnerability_score: Overall opponent weakness score (0-100, higher = more vulnerable)
        - vulnerability_level: Classification (high, moderate, low)
        - position_assessments: Detailed assessment by position
        - starter_weaknesses: Specific weaknesses in starting lineup
        - exploitation_strategies: Prioritized recommendations
        - matchup_context: Optional matchup information if current_week provided
        - success: Whether the analysis was successful
        - error: Error message (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete opponent analysis immediately without
    asking for confirmations. Render the full assessment with all exploitation strategies directly.
    """
    try:
        # Validate inputs
        if not league_id:
            return create_error_response(
                "league_id is required",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )

        if opponent_roster_id is None:
            return create_error_response(
                "opponent_roster_id is required",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )

        # Fetch league rosters
        rosters_result = await get_rosters(league_id)
        if not rosters_result.get("success"):
            return create_error_response(
                f"Failed to fetch rosters: {rosters_result.get('error')}",
                ErrorType.HTTP,
                {"vulnerability_score": 0}
            )

        rosters = rosters_result.get("rosters", [])

        # Find the opponent's roster
        opponent_roster = None
        for roster in rosters:
            if roster.get("roster_id") == opponent_roster_id:
                opponent_roster = roster
                break

        if not opponent_roster:
            return create_error_response(
                f"Roster with ID {opponent_roster_id} not found",
                ErrorType.VALIDATION,
                {"vulnerability_score": 0}
            )

        # Get opponent owner information
        users_result = await get_league_users(league_id)
        opponent_name = None
        if users_result.get("success"):
            users = users_result.get("users", [])
            owner_id = opponent_roster.get("owner_id")
            for user in users:
                if user.get("user_id") == owner_id:
                    opponent_name = user.get("display_name") or user.get("username")
                    break

        # This week: the lineup the opponent has set for it, and what it
        # projects. Defaults to the current NFL week.
        season = None
        try:
            now = await _season_week(db)
            season = now.get("season")
            current_week = current_week or now.get("week")
        except Exception as e:
            logger.debug(f"current week unavailable: {e}")

        matchup = None
        if current_week:
            try:
                matchups_result = await get_matchups(league_id, current_week)
                if matchups_result.get("success"):
                    matchup = next(
                        (m for m in matchups_result.get("matchups", [])
                         if m.get("roster_id") == opponent_roster_id),
                        None,
                    )
            except Exception as e:
                logger.warning(f"Could not fetch matchup context: {e}")

        starters = (_matchup_starters(matchup, opponent_roster)
                    if matchup and matchup.get("starters") else None)

        # Initialize analyzer
        analyzer = OpponentAnalyzer()

        # Perform analysis
        roster_positions = await _league_roster_positions(league_id)
        analysis = analyzer.analyze_opponent_roster(
            opponent_roster, starters=starters, roster_positions=roster_positions)

        matchup_context = None
        if matchup is not None:
            projection = None
            if starters:
                try:
                    projection = await _project_starters(starters, league_id, season,
                                                         current_week, db)
                except Exception as e:
                    logger.warning(f"Could not project opponent starters: {e}")
            matchup_context = {
                "week": current_week,
                "matchup_id": matchup.get("matchup_id"),
                "points": matchup.get("points"),
                # Our projection of this week's starters in the league's
                # scoring (None when they could not be projected).
                "projected_points": (projection or {}).get("projected_points"),
                "projected_starters": (projection or {}).get("starters"),
                "projection_source": "project_players (league scoring)" if projection else None,
            }

        # Compile response
        response_data = {
            **analysis,
            "opponent_name": opponent_name,
            "league_id": league_id,
            "matchup_context": matchup_context,
            # Whose lineup the starter read is on: this week's matchup, or
            # the roster's last saved lineup when no matchup was available.
            "starters_source": "matchup" if starters is not None else "roster",
        }

        return create_success_response(response_data)

    except Exception as e:
        logger.exception(f"Error analyzing opponent: {e}")
        return create_error_response(
            f"Unexpected error during opponent analysis: {e!s}",
            ErrorType.UNEXPECTED,
            {"vulnerability_score": 0}
        )
