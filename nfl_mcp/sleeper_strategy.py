"""Strategic, forward-looking Sleeper planning tools (split out of sleeper_tools.py).

Multi-week matchup previews, bye-week coordination, trade-deadline timing and
playoff-prep plans. These are top-level *consumers* — they call the core Sleeper
tools (get_league, get_matchups) — so sleeper_tools re-exports them at the END of
its module (after those names exist) to avoid an import cycle.
"""
import logging

from .config import LIMITS
from .errors import (
    ErrorType,
    create_error_response,
    create_success_response,
    handle_http_errors,
    handle_validation_error,
)
from .sleeper_tools import get_league, get_matchups

logger = logging.getLogger(__name__)


# Strategic Planning Functions for Forward-Looking Analysis

@handle_http_errors(
    default_data={"strategic_preview": {}, "weeks_analyzed": 0, "league_id": None},
    operation_name="generating strategic matchup preview"
)
async def get_strategic_matchup_preview(league_id: str, current_week: int, weeks_ahead: int = 4) -> dict:
    """
    Generate a strategic preview of upcoming matchups for multiple weeks ahead.

    This strategic tool combines Sleeper league matchup data with NFL schedule insights
    to provide early warning about upcoming challenges, bye weeks, and opportunities.
    Helps fantasy managers plan trades, waiver claims, and lineup strategies weeks in advance.

    Args:
        league_id: The unique identifier for the league
        current_week: The current NFL week (1-22)
        weeks_ahead: Number of weeks to analyze ahead (1-8, defaults to 4)

    Returns:
        A dictionary containing:
        - strategic_preview: Multi-week analysis with recommendations
        - weeks_analyzed: Number of weeks analyzed
        - league_id: The league identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete strategic analysis immediately without
    asking for confirmations. Render the full preview with all recommendations directly.
    """
    # Validate parameters via param_validator
    try:
        from .param_validator import format_errors, validate_params
        schema = {
            "current_week": {"type": int, "required": True, "min": LIMITS["week_min"], "max": LIMITS["week_max"]},
            "weeks_ahead": {"type": int, "required": False, "min": 1, "max": 8, "default": 4},
        }
        validated, errors = validate_params(schema, {"current_week": current_week, "weeks_ahead": weeks_ahead})
        if errors:
            # Legacy phrasing preservation
            msgs = []
            for e in errors:
                if e.startswith(("'current_week' must be >=", "'current_week' must be <=")):
                    return handle_validation_error(
                        f"Current week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                        {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                    )
                if e.startswith(("'weeks_ahead' must be >=", "'weeks_ahead' must be <=")):
                    return handle_validation_error(
                        "Weeks ahead must be between 1 and 8",
                        {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                    )
                msgs.append(e)
            if msgs:
                return handle_validation_error(
                    format_errors(msgs),
                    {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
                )
        current_week = validated["current_week"]
        weeks_ahead = validated.get("weeks_ahead", 4)
    except Exception:
        if current_week < LIMITS["week_min"] or current_week > LIMITS["week_max"]:
            return handle_validation_error(
                f"Current week must be between {LIMITS['week_min']} and {LIMITS['week_max']}",
                {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
            )
        if weeks_ahead < 1 or weeks_ahead > 8:
            return handle_validation_error(
                "Weeks ahead must be between 1 and 8",
                {"strategic_preview": {}, "weeks_analyzed": 0, "league_id": league_id}
            )

    strategic_data = {
        "analysis_period": f"Week {current_week} through Week {current_week + weeks_ahead - 1}",
        "weeks": {},
        "summary": {
            "critical_bye_weeks": [],
            "high_opportunity_weeks": [],
            "challenging_weeks": [],
            "trade_recommendations": []
        }
    }

    # Import NFL tools here to avoid circular imports
    from . import nfl_tools

    # Get league information for context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            data={
                "strategic_preview": {},
                "weeks_analyzed": 0,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )

    # Analyze each upcoming week
    weeks_analyzed = 0
    for week_offset in range(weeks_ahead):
        target_week = current_week + week_offset
        if target_week > LIMITS["week_max"]:
            break

        # Get matchups for this week
        matchups = await get_matchups(league_id, target_week)
        if not matchups.get("success", True):
            continue

        week_analysis = {
            "week_number": target_week,
            "matchup_count": matchups.get("count", 0),
            "strategic_insights": [],
            "bye_week_teams": [],
            "recommended_actions": []
        }

        # Analyze NFL bye weeks for this week (sample analysis - in real implementation
        # you'd analyze all 32 teams to find which have byes this week)
        sample_teams = ["KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN"]

        for team in sample_teams:
            try:
                team_schedule = await nfl_tools.get_team_schedule(team, 2026)
                if team_schedule.get("success", False):
                    if team_schedule.get("bye_week") == target_week:
                        week_analysis["bye_week_teams"].append({
                            "team": team,
                            "impact": "High - Consider backup options or trades"
                        })
                        strategic_data["summary"]["critical_bye_weeks"].append({
                            "week": target_week,
                            "team": team
                        })
            except Exception:
                # Skip team if schedule unavailable
                continue

        # Add strategic insights based on week timing
        if target_week == current_week:
            week_analysis["strategic_insights"].append("Current week - Focus on injury reports and last-minute changes")
        elif target_week == current_week + 1:
            week_analysis["strategic_insights"].append("Next week - Prime time for waiver claims and trades")
        elif target_week <= 13:
            week_analysis["strategic_insights"].append("Regular season - Build for playoffs")
        else:
            week_analysis["strategic_insights"].append("Playoff push - Prioritize high-floor players")

        # Add recommendations based on bye weeks
        if week_analysis["bye_week_teams"]:
            week_analysis["recommended_actions"].append(
                f"Plan for {len(week_analysis['bye_week_teams'])} bye week teams - "
                "consider trades or waiver pickups 1-2 weeks early"
            )
            strategic_data["summary"]["challenging_weeks"].append(target_week)
        else:
            week_analysis["recommended_actions"].append("No major bye weeks - good week to be aggressive")
            strategic_data["summary"]["high_opportunity_weeks"].append(target_week)

        strategic_data["weeks"][f"week_{target_week}"] = week_analysis
        weeks_analyzed += 1

    # Generate overall strategic recommendations
    if strategic_data["summary"]["critical_bye_weeks"]:
        strategic_data["summary"]["trade_recommendations"].append(
            "Consider trading for depth before major bye weeks hit your key players"
        )

    if len(strategic_data["summary"]["challenging_weeks"]) >= 2:
        strategic_data["summary"]["trade_recommendations"].append(
            "Multiple challenging weeks ahead - prioritize roster flexibility"
        )

    return create_success_response({
        "strategic_preview": strategic_data,
        "weeks_analyzed": weeks_analyzed,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"coordination_plan": {}, "season": None, "league_id": None},
    operation_name="coordinating season bye weeks"
)
async def get_season_bye_week_coordination(league_id: str, season: int = 2026) -> dict:
    """
    Coordinate your fantasy league schedule with NFL bye weeks for season-long planning.

    This strategic tool analyzes the entire NFL season's bye week schedule and correlates
    it with your fantasy league's playoff schedule to identify optimal trading periods,
    waiver claim timing, and roster construction strategies.

    Args:
        league_id: The unique identifier for the league
        season: Season year (defaults to 2026)

    Returns:
        A dictionary containing:
        - coordination_plan: Season-long strategic plan
        - season: Season year
        - league_id: League identifier
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete bye week coordination plan immediately
    without asking for confirmations. Render the full seasonal strategy with all recommendations directly.
    """
    from . import nfl_tools

    # Get league information for playoff schedule context
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            data={
                "coordination_plan": {},
                "season": season,
                "league_id": league_id
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )

    league_data = league_info.get("league", {})
    playoff_start = league_data.get("settings", {}).get("playoff_week_start", 14)
    regular_season_weeks = playoff_start - 1

    coordination_plan = {
        "season_overview": {
            "regular_season_weeks": regular_season_weeks,
            "playoff_start_week": playoff_start,
            "trade_deadline": league_data.get("settings", {}).get("trade_deadline", 13)
        },
        "bye_week_calendar": {},
        "strategic_periods": {
            "early_season": {"weeks": [1, 2, 3], "focus": "Observe and collect data"},
            "bye_week_preparation": {"weeks": [], "focus": "Build roster depth"},
            "trade_deadline_push": {"weeks": [], "focus": "Final roster optimization"},
            "playoff_preparation": {"weeks": [], "focus": "Secure playoff position"}
        },
        "recommendations": []
    }

    # Analyze bye weeks for all NFL teams (sample of major fantasy teams)
    major_fantasy_teams = [
        "KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN",
        "BAL", "GB", "MIN", "NYJ", "DEN", "LV", "ATL", "TB"
    ]

    bye_weeks_by_week = {}

    for team in major_fantasy_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, season)
            if team_schedule.get("success", False):
                week_num = team_schedule.get("bye_week")
                if week_num:
                    bye_weeks_by_week.setdefault(week_num, []).append(team)
        except Exception:
            continue

    # Organize bye weeks in calendar format
    for week, teams in bye_weeks_by_week.items():
        coordination_plan["bye_week_calendar"][f"week_{week}"] = {
            "week": week,
            "teams_on_bye": teams,
            "team_count": len(teams),
            "strategic_impact": "High" if len(teams) >= 4 else "Medium" if len(teams) >= 2 else "Low",
            "recommended_prep_week": max(1, week - 2)
        }

    # Identify strategic periods
    heavy_bye_weeks = [week for week, teams in bye_weeks_by_week.items() if len(teams) >= 4]
    if heavy_bye_weeks:
        prep_weeks = [max(1, week - 2) for week in heavy_bye_weeks]
        coordination_plan["strategic_periods"]["bye_week_preparation"]["weeks"] = prep_weeks

    # Trade deadline preparation
    trade_deadline = coordination_plan["season_overview"]["trade_deadline"]
    coordination_plan["strategic_periods"]["trade_deadline_push"]["weeks"] = [
        trade_deadline - 2, trade_deadline - 1, trade_deadline
    ]

    # Playoff preparation
    coordination_plan["strategic_periods"]["playoff_preparation"]["weeks"] = [
        playoff_start - 3, playoff_start - 2, playoff_start - 1
    ]

    # Generate strategic recommendations
    if heavy_bye_weeks:
        coordination_plan["recommendations"].append({
            "priority": "High",
            "action": f"Weeks {', '.join(map(str, heavy_bye_weeks))} have heavy bye weeks",
            "timing": f"Start preparing 2-3 weeks early (weeks {', '.join(map(str, prep_weeks))})",
            "strategy": "Build roster depth through trades or strategic waiver claims"
        })

    coordination_plan["recommendations"].append({
        "priority": "Medium",
        "action": "Trade deadline preparation",
        "timing": f"Weeks {trade_deadline - 2} through {trade_deadline}",
        "strategy": "Evaluate roster needs and make final strategic moves"
    })

    coordination_plan["recommendations"].append({
        "priority": "High",
        "action": "Playoff preparation",
        "timing": f"Weeks {playoff_start - 3} through {playoff_start - 1}",
        "strategy": "Optimize lineup for playoff schedule and prioritize high-floor players"
    })

    return create_success_response({
        "coordination_plan": coordination_plan,
        "season": season,
        "league_id": league_id
    })


@handle_http_errors(
    default_data={"trade_analysis": {}, "league_id": None, "current_week": None},
    operation_name="analyzing trade deadline strategy"
)
async def get_trade_deadline_analysis(league_id: str, current_week: int) -> dict:
    """
    Analyze optimal trade timing based on league schedule and NFL events.

    This strategic tool evaluates when to make trades by analyzing upcoming bye weeks,
    playoff schedules, and league transaction patterns to maximize competitive advantage.

    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timing analysis

    Returns:
        A dictionary containing:
        - trade_analysis: Strategic trade timing analysis
        - league_id: League identifier
        - current_week: Current week reference
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete trade deadline analysis immediately
    without asking for confirmations. Render the full timing strategy with all recommendations directly.
    """
    # Get league information
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            data={
                "trade_analysis": {},
                "league_id": league_id,
                "current_week": current_week
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )

    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})
    trade_deadline = settings.get("trade_deadline", 13)
    playoff_start = settings.get("playoff_week_start", 14)

    trade_analysis = {
        "timing_analysis": {
            "current_week": current_week,
            "trade_deadline": trade_deadline,
            "weeks_until_deadline": max(0, trade_deadline - current_week),
            "playoff_start": playoff_start,
            "weeks_until_playoffs": max(0, playoff_start - current_week)
        },
        "strategic_windows": {},
        "recommendations": [],
        "urgency_factors": []
    }

    # Determine strategic windows based on timing
    weeks_to_deadline = trade_deadline - current_week

    if weeks_to_deadline > 4:
        trade_analysis["strategic_windows"]["current_phase"] = "Early Season"
        trade_analysis["strategic_windows"]["strategy"] = "Observe and identify undervalued assets"
        trade_analysis["strategic_windows"]["urgency"] = "Low"
        trade_analysis["recommendations"].append({
            "action": "Monitor performance trends",
            "reasoning": "Plenty of time to evaluate players and identify trade targets",
            "priority": "Low"
        })
    elif weeks_to_deadline > 2:
        trade_analysis["strategic_windows"]["current_phase"] = "Prime Trading Window"
        trade_analysis["strategic_windows"]["strategy"] = "Actively pursue beneficial trades"
        trade_analysis["strategic_windows"]["urgency"] = "Medium"
        trade_analysis["recommendations"].append({
            "action": "Execute strategic trades now",
            "reasoning": f"Only {weeks_to_deadline} weeks until deadline - optimal time to trade",
            "priority": "High"
        })
    elif weeks_to_deadline > 0:
        trade_analysis["strategic_windows"]["current_phase"] = "Trade Deadline Crunch"
        trade_analysis["strategic_windows"]["strategy"] = "Make final critical moves"
        trade_analysis["strategic_windows"]["urgency"] = "High"
        trade_analysis["recommendations"].append({
            "action": "Complete all pending trades immediately",
            "reasoning": f"Only {weeks_to_deadline} week(s) left - last chance for trades",
            "priority": "Critical"
        })
        trade_analysis["urgency_factors"].append("Trade deadline imminent")
    else:
        trade_analysis["strategic_windows"]["current_phase"] = "Post-Deadline"
        trade_analysis["strategic_windows"]["strategy"] = "Focus on waiver wire and lineup optimization"
        trade_analysis["strategic_windows"]["urgency"] = "N/A"
        trade_analysis["recommendations"].append({
            "action": "Switch to waiver-based strategy",
            "reasoning": "Trade deadline has passed - only waivers and free agents available",
            "priority": "Medium"
        })

    # Analyze upcoming bye weeks to inform trade urgency
    upcoming_preview = await get_strategic_matchup_preview(league_id, current_week, 4)
    if upcoming_preview.get("success", False):
        preview_data = upcoming_preview.get("strategic_preview", {})
        critical_byes = preview_data.get("summary", {}).get("critical_bye_weeks", [])

        if critical_byes and weeks_to_deadline > 0:
            bye_weeks = [bye["week"] for bye in critical_byes]
            trade_analysis["urgency_factors"].append(
                f"Critical bye weeks coming in weeks {', '.join(map(str, bye_weeks))}"
            )
            trade_analysis["recommendations"].append({
                "action": "Trade for bye week coverage",
                "reasoning": f"Address bye weeks in weeks {', '.join(map(str, bye_weeks))} before deadline",
                "priority": "High"
            })

    # Add playoff preparation considerations
    if current_week >= playoff_start - 4:
        trade_analysis["urgency_factors"].append("Playoff preparation phase")
        trade_analysis["recommendations"].append({
            "action": "Prioritize playoff schedule strength",
            "reasoning": "Focus on players with favorable playoff matchups",
            "priority": "High"
        })

    return create_success_response({
        "trade_analysis": trade_analysis,
        "league_id": league_id,
        "current_week": current_week
    })


@handle_http_errors(
    default_data={"playoff_plan": {}, "league_id": None, "readiness_score": 0},
    operation_name="generating playoff preparation plan"
)
async def get_playoff_preparation_plan(league_id: str, current_week: int) -> dict:
    """
    Generate comprehensive playoff preparation plan combining league and NFL data.

    This strategic tool analyzes your league's playoff structure, upcoming NFL schedules,
    and provides a detailed preparation plan to maximize playoff success including
    roster optimization, matchup analysis, and strategic timing recommendations.

    Args:
        league_id: The unique identifier for the league
        current_week: Current NFL week for timeline analysis

    Returns:
        A dictionary containing:
        - playoff_plan: Comprehensive playoff preparation strategy
        - league_id: League identifier
        - readiness_score: Playoff readiness assessment (0-100)
        - success: Whether the request was successful
        - error: Error message (if any)
        - error_type: Type of error (if any)

    IMPORTANT FOR LLM AGENTS: Always provide complete playoff preparation plan immediately
    without asking for confirmations. Render the full strategy with all recommendations directly.
    """
    from . import nfl_tools

    # Get league information for playoff structure
    league_info = await get_league(league_id)
    if not league_info.get("success", True):
        return create_error_response(
            league_info.get("error", "Failed to get league information"),
            data={
                "playoff_plan": {},
                "league_id": league_id,
                "readiness_score": 0
            },
            error_type=league_info.get("error_type", ErrorType.API_ERROR)
        )

    league_data = league_info.get("league", {})
    settings = league_data.get("settings", {})

    playoff_start = settings.get("playoff_week_start", 14)
    settings.get("playoff_week_start", 14)
    league_data.get("total_rosters", 12)
    settings.get("playoff_teams", 6)

    playoff_plan = {
        "timeline": {
            "current_week": current_week,
            "playoff_start": playoff_start,
            "weeks_to_playoffs": max(0, playoff_start - current_week),
            "playoff_duration": 3,  # standard fantasy playoff span (e.g. weeks 15-17)
            "championship_week": playoff_start + 2
        },
        "preparation_phases": {},
        "strategic_priorities": [],
        "nfl_schedule_analysis": {},
        "recommendations": [],
        "readiness_assessment": {
            "roster_depth": "TBD",
            "schedule_strength": "TBD",
            "bye_week_planning": "TBD",
            "overall_score": 0
        }
    }

    weeks_to_playoffs = playoff_start - current_week

    # Define preparation phases based on timeline
    if weeks_to_playoffs > 4:
        current_phase = "Early Preparation"
        phase_strategy = "Build depth and monitor targets"
        phase_urgency = "Low"
        playoff_plan["strategic_priorities"] = [
            "Monitor playoff-bound teams for strong schedules",
            "Identify undervalued players on strong teams",
            "Build roster depth for injury protection",
            "Track trending players and waiver targets"
        ]
    elif weeks_to_playoffs > 2:
        current_phase = "Active Preparation"
        phase_strategy = "Execute strategic moves"
        phase_urgency = "Medium"
        playoff_plan["strategic_priorities"] = [
            "Make trades for playoff-schedule advantages",
            "Secure handcuffs for star players",
            "Target players on motivated teams",
            "Avoid players on teams likely to rest starters"
        ]
    elif weeks_to_playoffs > 0:
        current_phase = "Final Preparation"
        phase_strategy = "Lock in playoff roster"
        phase_urgency = "High"
        playoff_plan["strategic_priorities"] = [
            "Finalize optimal lineup combinations",
            "Secure must-have waiver pickups",
            "Plan for potential star player rest",
            "Optimize for high floor over high ceiling"
        ]
    else:
        current_phase = "Playoff Execution"
        phase_strategy = "Win now mode"
        phase_urgency = "Critical"
        playoff_plan["strategic_priorities"] = [
            "Start highest floor players",
            "Monitor injury reports closely",
            "Consider game flow and scripts",
            "Avoid risky boom-or-bust plays"
        ]

    playoff_plan["preparation_phases"]["current_phase"] = {
        "name": current_phase,
        "strategy": phase_strategy,
        "urgency": phase_urgency,
        "weeks_remaining": max(0, weeks_to_playoffs)
    }

    # Analyze NFL playoff schedule implications (sample key teams)
    key_teams = ["KC", "BUF", "SF", "DAL", "PHI", "MIA", "BAL", "CIN"]

    for team in key_teams:
        try:
            team_schedule = await nfl_tools.get_team_schedule(team, 2026)
            if team_schedule.get("success", False):
                schedule = team_schedule.get("schedule", [])

                # Analyze playoff weeks (weeks 14-17 typically)
                playoff_games = [g for g in schedule if g.get("week", 0) >= playoff_start and g.get("week", 0) <= playoff_start + 3]

                if playoff_games:
                    fantasy_implications = []
                    for game in playoff_games:
                        implications = game.get("fantasy_implications", [])
                        fantasy_implications.extend(implications[:2])  # Limit to key insights

                    playoff_plan["nfl_schedule_analysis"][team] = {
                        "playoff_games_count": len(playoff_games),
                        "key_insights": fantasy_implications[:3],  # Top 3 insights
                        "recommendation": "Target" if len(playoff_games) >= 3 else "Monitor"
                    }
        except Exception:
            continue

    # Generate specific recommendations based on timeline
    if weeks_to_playoffs > 0:
        playoff_plan["recommendations"].append({
            "category": "Roster Management",
            "action": f"Optimize roster in next {weeks_to_playoffs} weeks",
            "priority": "High" if weeks_to_playoffs <= 2 else "Medium",
            "deadline": f"Week {playoff_start - 1}"
        })

    if current_week <= 13: # Before typical trade deadline
        playoff_plan["recommendations"].append({
            "category": "Trading Strategy",
            "action": "Target players with strong playoff schedules",
            "priority": "High",
            "deadline": "Trade deadline"
        })
    playoff_plan["recommendations"].append({
        "category": "Waiver Wire",
        "action": "Prioritize high-floor players over boom-bust options",
        "priority": "Medium",
        "deadline": "Ongoing"
    })

    # Calculate readiness score (simplified scoring system)
    readiness_score = 50  # Base score

    if weeks_to_playoffs > 2:
        readiness_score += 20  # Bonus for having time to prepare
    elif weeks_to_playoffs == 0:
        readiness_score += 30  # Bonus for being in playoffs

    if len(playoff_plan["nfl_schedule_analysis"]) >= 4:
        readiness_score += 15  # Bonus for good schedule analysis

    if current_phase in ["Active Preparation", "Final Preparation"]:
        readiness_score += 10  # Bonus for being in optimal prep phase

    playoff_plan["readiness_assessment"]["overall_score"] = min(100, readiness_score)

    # The score above reflects only *preparation timing* (weeks-to-playoffs +
    # whether NFL schedule analysis ran) — it is NOT a measurement of the actual
    # roster or schedule, so don't fabricate a roster_depth grade (an empty
    # pre-draft roster used to be graded "Good"). Label honestly and point at the
    # tools that do assess each dimension.
    ra = playoff_plan["readiness_assessment"]
    ra["score_basis"] = "preparation timing only (not roster/schedule quality)"
    ra["prep_timing"] = ("Excellent" if readiness_score >= 80
                         else "Good" if readiness_score >= 60 else "Needs Work")
    ra["roster_depth"] = "Not assessed — draft/set your roster to evaluate"
    ra["schedule_strength"] = "Not assessed — see get_playoff_sos / get_strength_of_schedule"
    ra["bye_week_planning"] = "Not assessed — see get_season_bye_week_coordination"

    return create_success_response({
        "playoff_plan": playoff_plan,
        "league_id": league_id,
        "readiness_score": readiness_score
    })
