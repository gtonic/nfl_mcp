# Fantasy Intelligence API Usage Examples

This document provides practical examples of how to use the new Fantasy Intelligence APIs to make informed fantasy football decisions.

## Quick Start Examples

### 1. Pre-Game Lineup Decisions

```python
# Check injury status before setting your lineup
injuries = await client.call_tool("get_team_injuries", {"team_id": "KC", "limit": 20})

for injury in injuries.data["injuries"]:
    if injury["severity"] == "High":
        print(f"⚠️  AVOID: {injury['player_name']} ({injury['position']}) - {injury['status']}")
    elif injury["severity"] == "Medium":
        print(f"⚡ RISKY: {injury['player_name']} ({injury['position']}) - {injury['status']}")
```

### 2. Waiver Wire Research

```python
# Analyze team motivation for potential pickups
standings = await client.call_tool("get_nfl_standings", {"season": 2025})

for team in standings.data["standings"]:
    if team["motivation_level"] == "High (Playoff hunt)":
        print(f"🔥 {team['abbreviation']}: {team['fantasy_context']}")
        
        # Get their players to see who might get more opportunities
        players = await client.call_tool("get_team_player_stats", {"team_id": team["abbreviation"]})
        fantasy_players = [p for p in players.data["player_stats"] if p["fantasy_relevant"]]
        print(f"   Fantasy relevant players: {len(fantasy_players)}")

# Enhanced waiver wire analysis with de-duplication
waiver_dashboard = await client.call_tool("get_waiver_wire_dashboard", {"league_id": "your_league_id"})

if waiver_dashboard.data["success"]:
    summary = waiver_dashboard.data["dashboard_summary"]
    print(f"📊 Waiver Wire Dashboard:")
    print(f"   Total transactions: {summary['total_waiver_transactions']}")
    print(f"   Duplicates removed: {summary['duplicates_removed']}")
    print(f"   Volatile players: {summary['volatile_players_count']}")
    
    # Check for volatile players (dropped and re-added)
    if waiver_dashboard.data["volatile_players"]:
        print("⚠️  Volatile players to avoid:")
        for player_id in waiver_dashboard.data["volatile_players"]:
            print(f"   - Player ID: {player_id}")
```

### 3. Matchup Analysis

```python
# Analyze upcoming matchups for your players
schedule = await client.call_tool("get_team_schedule", {"team_id": "KC", "season": 2025})

upcoming_games = [g for g in schedule.data["schedule"] if g["result"] == "scheduled"][:4]

for game in upcoming_games:
    opponent = game["opponent"]["abbreviation"]
    home_away = "vs" if game["is_home"] else "@"
    
    print(f"Week {game['week']}: {home_away} {opponent}")
    for implication in game["fantasy_implications"]:
        print(f"  💡 {implication}")
```

### Waiver Wire Intelligence

```python
async def comprehensive_waiver_analysis(league_id):
    """Comprehensive waiver wire analysis with de-duplication and re-entry tracking."""
    
    print("🔍 Waiver Wire Intelligence Report")
    print("=" * 50)
    
    # Get comprehensive waiver dashboard
    dashboard = await client.call_tool("get_waiver_wire_dashboard", {"league_id": league_id})
    
    if not dashboard.data["success"]:
        print(f"❌ Error: {dashboard.data['error']}")
        return
    
    summary = dashboard.data["dashboard_summary"]
    
    # Summary statistics
    print("\n📊 Summary Statistics:")
    print(f"   Total waiver transactions: {summary['total_waiver_transactions']}")
    print(f"   Unique transactions (after dedup): {summary['unique_transactions']}")
    print(f"   Duplicates removed: {summary['duplicates_removed']}")
    print(f"   Deduplication rate: {summary['deduplication_rate']:.1f}%")
    print(f"   Players analyzed: {summary['total_players_analyzed']}")
    
    # Re-entry analysis
    print(f"\n🔄 Re-Entry Analysis:")
    print(f"   Players with re-entries: {summary['players_with_re_entries']}")
    print(f"   Volatile players (multiple re-entries): {summary['volatile_players_count']}")
    
    # Show volatile players detail
    if dashboard.data["volatile_players"]:
        print("\n⚠️  Volatile Players to Monitor:")
        re_entry_data = dashboard.data["re_entry_analysis"]
        
        for player_id in dashboard.data["volatile_players"]:
            if player_id in re_entry_data:
                player_info = re_entry_data[player_id]
                re_entries = len(player_info["re_entries"])
                print(f"   🔴 Player {player_id}: {re_entries} re-entries")
                print(f"      Total activities: {player_info['total_activities']}")
                print(f"      Current status: {player_info['latest_status']}")
    
    # Show recent clean waiver activity
    print(f"\n✅ Clean Waiver Activity (Last 10):")
    waiver_log = dashboard.data["waiver_log"][-10:]  # Last 10 transactions
    
    for tx in waiver_log:
        tx_type = tx.get("type", "unknown")
        adds = tx.get("adds", {})
        drops = tx.get("drops", {})
        
        if adds:
            for player_id in adds.keys():
                print(f"   ➕ {tx_type}: Player {player_id} added")
        
        if drops:
            for player_id in drops.keys():
                print(f"   ➖ {tx_type}: Player {player_id} dropped")

async def focused_re_entry_check(league_id):
    """Check specific players for re-entry patterns."""
    
    print("🔄 Re-Entry Status Check")
    print("=" * 30)
    
    re_entry_result = await client.call_tool("check_re_entry_status", {"league_id": league_id})
    
    if not re_entry_result.data["success"]:
        print(f"❌ Error: {re_entry_result.data['error']}")
        return
    
    re_entry_players = re_entry_result.data["re_entry_players"]
    
    if not re_entry_players:
        print("✅ No re-entry patterns detected - all waiver activity looks stable")
        return
    
    print(f"Found {len(re_entry_players)} players with re-entry patterns:")
    
    for player_id, analysis in re_entry_players.items():
        print(f"\n🔍 Player {player_id}:")
        print(f"   Activities: {analysis['total_activities']}")
        print(f"   Drops: {analysis['drops_count']}, Adds: {analysis['adds_count']}")
        print(f"   Volatile: {'⚠️ YES' if analysis['is_volatile'] else '✅ No'}")
        
        for i, re_entry in enumerate(analysis['re_entries'], 1):
            days = re_entry.get('days_between', 0) or 0
            same_roster = "🔄 Same team" if re_entry['same_roster'] else "↔️  Different team"
            print(f"   Re-entry {i}: {days:.1f} days between drop/add - {same_roster}")

# Usage examples
await comprehensive_waiver_analysis("your_league_id")
await focused_re_entry_check("your_league_id")
```

### Advanced Decision Making Workflows

### Weekly Lineup Strategy

```python
async def analyze_weekly_lineup(my_players_teams):
    """Analyze all your players' teams for the upcoming week."""
    
    analysis = {}
    
    for team_id in my_players_teams:
        # Get injury report
        injuries = await client.call_tool("get_team_injuries", {"team_id": team_id})
        
        # Get team schedule  
        schedule = await client.call_tool("get_team_schedule", {"team_id": team_id})
        
        # Get team motivation from standings
        standings = await client.call_tool("get_nfl_standings", {"season": 2025})
        team_standing = next((t for t in standings.data["standings"] 
                            if t["abbreviation"] == team_id), None)
        
        analysis[team_id] = {
            "injuries": len([i for i in injuries.data["injuries"] if i["severity"] in ["High", "Medium"]]),
            "next_game": next((g for g in schedule.data["schedule"] if g["result"] == "scheduled"), None),
            "motivation": team_standing["motivation_level"] if team_standing else "Unknown"
        }
    
    return analysis

# Usage
my_teams = ["KC", "BUF", "SF", "DAL"]  # Teams of your key players
weekly_analysis = await analyze_weekly_lineup(my_teams)

for team, info in weekly_analysis.items():
    print(f"\n{team} Analysis:")
    print(f"  Concerning injuries: {info['injuries']}")
    print(f"  Motivation level: {info['motivation']}")
    if info['next_game']:
        opponent = info['next_game']['opponent']['abbreviation'] 
        home_away = "vs" if info['next_game']['is_home'] else "@"
        print(f"  Next: Week {info['next_game']['week']} {home_away} {opponent}")
```

### Bye Week Planning

```python
async def plan_bye_weeks(season=2025):
    """Identify all bye weeks to plan ahead."""
    
    all_teams = ["KC", "BUF", "SF", "DAL", "LAR", "TB", "GB", "NE"]  # Add more as needed
    bye_week_schedule = {}
    
    for team in all_teams:
        schedule = await client.call_tool("get_team_schedule", {"team_id": team, "season": season})
        
        for game in schedule.data["schedule"]:
            if "BYE WEEK" in game.get("fantasy_implications", []):
                bye_week_schedule[team] = game["week"]
                break
    
    # Group by week
    by_week = {}
    for team, week in bye_week_schedule.items():
        if week not in by_week:
            by_week[week] = []
        by_week[week].append(team)
    
    for week in sorted(by_week.keys()):
        print(f"Week {week} Bye: {', '.join(by_week[week])}")
    
    return bye_week_schedule
```

### Injury Monitoring Dashboard

```python
async def injury_monitoring_dashboard(teams_to_monitor):
    """Create a comprehensive injury monitoring dashboard."""
    
    print("🏥 INJURY MONITORING DASHBOARD")
    print("=" * 50)
    
    all_injuries = {}
    
    for team in teams_to_monitor:
        injuries = await client.call_tool("get_team_injuries", {"team_id": team})
        
        if injuries.data["injuries"]:
            all_injuries[team] = injuries.data["injuries"]
            
            print(f"\n🏈 {team} - {injuries.data['team_name']}")
            print(f"   Total injuries: {len(injuries.data['injuries'])}")
            
            # Group by severity
            high_severity = [i for i in injuries.data["injuries"] if i["severity"] == "High"]
            medium_severity = [i for i in injuries.data["injuries"] if i["severity"] == "Medium"]
            
            if high_severity:
                print("   🔴 HIGH IMPACT:")
                for injury in high_severity:
                    print(f"      {injury['player_name']} ({injury['position']}) - {injury['status']}")
            
            if medium_severity:
                print("   🟡 MODERATE IMPACT:")
                for injury in medium_severity:
                    print(f"      {injury['player_name']} ({injury['position']}) - {injury['status']}")
    
    return all_injuries

# Usage - monitor your players' teams
my_fantasy_teams = ["KC", "BUF", "SF", "DAL", "LAR"]
injury_report = await injury_monitoring_dashboard(my_fantasy_teams)
```

### Playoff Push Analysis

```python
async def playoff_push_analysis(season=2025):
    """Identify teams in playoff hunt vs those likely to rest starters."""
    
    standings = await client.call_tool("get_nfl_standings", {"season": season})
    
    playoff_hunters = []
    rest_candidates = []
    
    for team in standings.data["standings"]:
        if team["motivation_level"] == "High (Playoff hunt)":
            playoff_hunters.append(team)
        elif team["motivation_level"] == "Low (Playoff lock)":
            rest_candidates.append(team)
    
    print("🔥 PLAYOFF HUNTERS (Start their players):")
    for team in playoff_hunters:
        print(f"   {team['abbreviation']} ({team['wins']}-{team['losses']}) - {team['fantasy_context']}")
    
    print("\n⚠️  REST CANDIDATES (Avoid their players late season):")
    for team in rest_candidates:
        print(f"   {team['abbreviation']} ({team['wins']}-{team['losses']}) - {team['fantasy_context']}")
    
    return {"playoff_hunters": playoff_hunters, "rest_candidates": rest_candidates}
```

## Daily/Weekly Routine Examples

### Daily Check (Game Days)

```python
async def daily_fantasy_check():
    """Quick daily check for any breaking injury news."""
    
    # Your key players' teams
    priority_teams = ["KC", "BUF", "SF"]  # Adjust for your roster
    
    print("📱 Daily Fantasy Check")
    print("-" * 30)
    
    for team in priority_teams:
        injuries = await client.call_tool("get_team_injuries", {"team_id": team, "limit": 5})
        
        # Check for any high-severity injuries
        urgent_injuries = [i for i in injuries.data["injuries"] if i["severity"] == "High"]
        
        if urgent_injuries:
            print(f"🚨 {team} URGENT:")
            for injury in urgent_injuries:
                print(f"   {injury['player_name']} ({injury['position']}) - {injury['status']}")
        else:
            print(f"✅ {team} - No urgent injury concerns")
```

### Weekly Planning Session

```python
async def weekly_planning_session(week_number):
    """Comprehensive weekly fantasy planning."""
    
    print(f"📊 Week {week_number} Fantasy Planning Session")
    print("=" * 50)
    
    # 1. League-wide injury scan
    print("\n1. League Injury Scan...")
    # Monitor top 10 fantasy teams
    top_teams = ["KC", "BUF", "SF", "DAL", "LAR", "PHI", "MIA", "CIN", "BAL", "GB"]
    
    for team in top_teams[:5]:  # Sample first 5
        injuries = await client.call_tool("get_team_injuries", {"team_id": team, "limit": 3})
        fantasy_injuries = [i for i in injuries.data["injuries"] 
                          if i["position"] in ["QB", "RB", "WR", "TE"] and i["severity"] != "Low"]
        
        if fantasy_injuries:
            print(f"   {team}: {len(fantasy_injuries)} fantasy-relevant injuries")
    
    # 2. Standings check for motivation
    print("\n2. Team Motivation Analysis...")
    standings = await client.call_tool("get_nfl_standings")
    
    high_motivation = [t for t in standings.data["standings"] 
                      if t["motivation_level"] == "High (Playoff hunt)"][:3]
    
    for team in high_motivation:
        print(f"   🔥 {team['abbreviation']}: High motivation - good for fantasy")
    
    # 3. Schedule look-ahead
    print(f"\n3. Week {week_number + 1} Preview...")
    preview_team = "KC"  # Example team
    schedule = await client.call_tool("get_team_schedule", {"team_id": preview_team})
    
    next_games = [g for g in schedule.data["schedule"] 
                 if g["result"] == "scheduled"][:2]
    
    for game in next_games:
        opponent = game["opponent"]["abbreviation"]
        home_away = "vs" if game["is_home"] else "@"
        print(f"   {preview_team} Week {game['week']}: {home_away} {opponent}")
        for implication in game["fantasy_implications"][:1]:  # First implication
            print(f"      💡 {implication}")

# Usage
await weekly_planning_session(week_number=15)
```

## Integration with Existing APIs

### Combine with Sleeper Data

```python
async def enhanced_roster_analysis(league_id, user_id):
    """Combine new APIs with existing Sleeper data for comprehensive analysis."""
    
    # Get your current roster from Sleeper
    rosters = await client.call_tool("get_rosters", {"league_id": league_id})
    your_roster = next((r for r in rosters.data["rosters"] if r["owner_id"] == user_id), None)
    
    if not your_roster:
        return "Roster not found"
    
    # Map player IDs to teams (this would require additional logic in real implementation)
    # For example purposes, assume we have this mapping
    player_teams = {
        "4881": "KC",  # Mahomes
        "4035687": "KC",  # Kelce
        # ... more mappings
    }
    
    print("🏈 Enhanced Roster Analysis")
    print("-" * 40)
    
    for player_id in your_roster["players"]:
        if player_id in player_teams:
            team = player_teams[player_id]
            
            # Get injury report for player's team
            injuries = await client.call_tool("get_team_injuries", {"team_id": team})
            
            # Check if this specific player is injured
            player_injury = next((i for i in injuries.data["injuries"] 
                                if player_id in str(i.get("player_id", ""))), None)
            
            if player_injury:
                print(f"⚠️  Player {player_id} ({team}): {player_injury['status']}")
            else:
                print(f"✅ Player {player_id} ({team}): Healthy")
```

## Best Practices

### 1. Rate Limiting Awareness
- Don't call APIs too frequently
- Cache results when appropriate
- Batch requests when possible

### 2. Data Freshness
- Injury reports change frequently - check daily
- Standings update weekly during season
- Schedules are mostly static but check for changes

### 3. Fantasy Context Interpretation
- High severity injuries = likely sit
- Medium severity = monitor closely
- Low motivation teams late season = consider alternatives
- Playoff hunters = prioritize their players

### 4. Error Handling
```python
async def safe_api_call(tool_name, params):
    """Safely call API with error handling."""
    try:
        result = await client.call_tool(tool_name, params)
        if not result.data.get("success", True):
            print(f"API Error: {result.data.get('error', 'Unknown error')}")
            return None
        return result.data
    except Exception as e:
        print(f"Network Error: {e}")
        return None

# Usage
injuries = await safe_api_call("get_team_injuries", {"team_id": "KC"})
if injuries:
    # Process data safely
    pass
```

These examples show how to leverage the new Fantasy Intelligence APIs to make data-driven decisions that can help you "win the fantasy league by having an always up to date team with strong players."

## Strategic Sleeper API Usage

The following examples demonstrate the strategic use of Sleeper API tools to plan for future events (matchups, bye weeks) early enough for competitive advantage.

### Multi-Week Strategic Preview

```python
async def strategic_season_planning(league_id, current_week=8):
    """Use Sleeper API strategically to plan multiple weeks ahead."""
    
    print(f"📊 Strategic Season Planning - Starting Week {current_week}")
    print("=" * 60)
    
    # Get strategic preview for next 6 weeks
    preview = await client.call_tool("get_strategic_matchup_preview", {
        "league_id": league_id,
        "current_week": current_week,
        "weeks_ahead": 6
    })
    
    if preview.data.get("success"):
        strategic_data = preview.data["strategic_preview"]
        
        print(f"\n📈 Analysis Period: {strategic_data['analysis_period']}")
        
        # Show critical bye weeks coming up
        critical_byes = strategic_data["summary"]["critical_bye_weeks"]
        if critical_byes:
            print(f"\n🚨 CRITICAL BYE WEEKS AHEAD:")
            for bye in critical_byes:
                print(f"   Week {bye['week']}: {bye['team']} on bye")
        
        # Show high opportunity weeks
        opportunity_weeks = strategic_data["summary"]["high_opportunity_weeks"]
        if opportunity_weeks:
            print(f"\n🎯 HIGH OPPORTUNITY WEEKS: {', '.join(map(str, opportunity_weeks))}")
            print("   → Good weeks to be aggressive with lineup decisions")
        
        # Show challenging weeks
        challenging_weeks = strategic_data["summary"]["challenging_weeks"]
        if challenging_weeks:
            print(f"\n⚠️  CHALLENGING WEEKS: {', '.join(map(str, challenging_weeks))}")
            print("   → Start planning backup options now")
        
        # Show trade recommendations
        trade_recs = strategic_data["summary"]["trade_recommendations"]
        for rec in trade_recs:
            print(f"\n💡 STRATEGIC RECOMMENDATION: {rec}")
    
    return preview.data

# Usage
await strategic_season_planning("your_league_id_here", current_week=8)
```

### Season-Long Bye Week Coordination

```python
async def master_bye_week_strategy(league_id, season=2025):
    """Coordinate entire season bye week planning with your league schedule."""
    
    coordination = await client.call_tool("get_season_bye_week_coordination", {
        "league_id": league_id,
        "season": season
    })
    
    if coordination.data.get("success"):
        plan = coordination.data["coordination_plan"]
        
        print("🗓️  SEASON BYE WEEK MASTER PLAN")
        print("=" * 50)
        
        # Show season overview
        overview = plan["season_overview"]
        print(f"Regular Season: Weeks 1-{overview['regular_season_weeks']}")
        print(f"Playoffs Start: Week {overview['playoff_start_week']}")
        print(f"Trade Deadline: Week {overview['trade_deadline']}")
        
        # Show bye week calendar
        print(f"\n📅 BYE WEEK CALENDAR:")
        bye_calendar = plan["bye_week_calendar"]
        for week_key in sorted(bye_calendar.keys(), key=lambda x: int(x.split('_')[1])):
            week_data = bye_calendar[week_key]
            impact = week_data["strategic_impact"]
            teams = ", ".join(week_data["teams_on_bye"])
            print(f"   Week {week_data['week']}: {teams} ({impact} impact)")
            print(f"      → Start preparing by Week {week_data['recommended_prep_week']}")
        
        # Show strategic periods
        print(f"\n🎯 KEY STRATEGIC PERIODS:")
        periods = plan["strategic_periods"]
        for period_name, period_data in periods.items():
            if period_data["weeks"]:
                weeks_str = ", ".join(map(str, period_data["weeks"]))
                print(f"   {period_name.replace('_', ' ').title()}: Weeks {weeks_str}")
                print(f"      Focus: {period_data['focus']}")
        
        # Show recommendations
        print(f"\n📋 STRATEGIC RECOMMENDATIONS:")
        for i, rec in enumerate(plan["recommendations"], 1):
            print(f"   {i}. {rec['action']} ({rec['priority']} Priority)")
            print(f"      Timing: {rec['timing']}")
            print(f"      Strategy: {rec['strategy']}")
    
    return coordination.data

# Usage  
await master_bye_week_strategy("your_league_id_here", 2025)
```

### Trade Deadline Strategic Analysis

```python
async def optimize_trade_deadline_timing(league_id, current_week):
    """Analyze optimal trade timing before deadline."""
    
    analysis = await client.call_tool("get_trade_deadline_analysis", {
        "league_id": league_id,
        "current_week": current_week
    })
    
    if analysis.data.get("success"):
        trade_data = analysis.data["trade_analysis"]
        
        print("📈 TRADE DEADLINE STRATEGIC ANALYSIS")
        print("=" * 45)
        
        # Show timing analysis
        timing = trade_data["timing_analysis"]
        print(f"Current Week: {timing['current_week']}")
        print(f"Trade Deadline: Week {timing['trade_deadline']}")
        print(f"Weeks Until Deadline: {timing['weeks_until_deadline']}")
        print(f"Weeks Until Playoffs: {timing['weeks_until_playoffs']}")
        
        # Show current strategic window
        windows = trade_data["strategic_windows"]
        print(f"\n🎯 CURRENT PHASE: {windows['current_phase']}")
        print(f"Strategy: {windows['strategy']}")
        print(f"Urgency Level: {windows['urgency']}")
        
        # Show urgency factors
        urgency_factors = trade_data.get("urgency_factors", [])
        if urgency_factors:
            print(f"\n🚨 URGENCY FACTORS:")
            for factor in urgency_factors:
                print(f"   • {factor}")
        
        # Show specific recommendations
        print(f"\n💡 ACTIONABLE RECOMMENDATIONS:")
        for i, rec in enumerate(trade_data["recommendations"], 1):
            print(f"   {i}. {rec['action']} ({rec['priority']} Priority)")
            print(f"      Reasoning: {rec['reasoning']}")
    
    return analysis.data

# Usage
await optimize_trade_deadline_timing("your_league_id_here", current_week=11)
```

### Comprehensive Playoff Preparation

```python
async def playoff_readiness_assessment(league_id, current_week):
    """Get comprehensive playoff preparation plan with readiness score."""
    
    prep_plan = await client.call_tool("get_playoff_preparation_plan", {
        "league_id": league_id,
        "current_week": current_week
    })
    
    if prep_plan.data.get("success"):
        plan = prep_plan.data["playoff_plan"]
        score = prep_plan.data["readiness_score"]
        
        print(f"🏆 PLAYOFF PREPARATION PLAN (Readiness: {score}/100)")
        print("=" * 55)
        
        # Show timeline
        timeline = plan["timeline"]
        print(f"📅 TIMELINE:")
        print(f"   Current Week: {timeline['current_week']}")
        print(f"   Playoff Start: Week {timeline['playoff_start']}")
        print(f"   Weeks to Playoffs: {timeline['weeks_to_playoffs']}")
        print(f"   Championship Week: {timeline['championship_week']}")
        
        # Show current preparation phase
        phase = plan["preparation_phases"]["current_phase"]
        print(f"\n🎯 CURRENT PHASE: {phase['name']}")
        print(f"   Strategy: {phase['strategy']}")
        print(f"   Urgency: {phase['urgency']}")
        print(f"   Weeks Remaining: {phase['weeks_remaining']}")
        
        # Show strategic priorities
        print(f"\n📋 STRATEGIC PRIORITIES:")
        for i, priority in enumerate(plan["strategic_priorities"], 1):
            print(f"   {i}. {priority}")
        
        # Show NFL schedule analysis
        nfl_analysis = plan["nfl_schedule_analysis"]
        if nfl_analysis:
            print(f"\n🏈 NFL SCHEDULE INSIGHTS:")
            for team, data in nfl_analysis.items():
                print(f"   {team}: {data['recommendation']} ({data['playoff_games_count']} playoff games)")
                if data["key_insights"]:
                    for insight in data["key_insights"][:2]:  # Top 2 insights
                        print(f"      • {insight}")
        
        # Show recommendations
        print(f"\n💡 RECOMMENDATIONS:")
        for rec in plan["recommendations"]:
            print(f"   📌 {rec['category']}: {rec['action']}")
            print(f"      Priority: {rec['priority']} | Deadline: {rec['deadline']}")
        
        # Show readiness assessment
        assessment = plan["readiness_assessment"]
        print(f"\n📊 READINESS BREAKDOWN:")
        print(f"   Roster Depth: {assessment['roster_depth']}")
        print(f"   Schedule Strength: {assessment['schedule_strength']}")
        print(f"   Bye Week Planning: {assessment['bye_week_planning']}")
        print(f"   Overall Score: {assessment['overall_score']}/100")
    
    return prep_plan.data

# Usage
await playoff_readiness_assessment("your_league_id_here", current_week=12)
```

### Combined Strategic Workflow

```python
async def full_strategic_analysis(league_id, current_week):
    """Complete strategic analysis combining all Sleeper API tools."""
    
    print("🚀 COMPREHENSIVE STRATEGIC ANALYSIS")
    print("=" * 50)
    
    # 1. Multi-week preview
    print("\n1️⃣ UPCOMING WEEKS PREVIEW")
    print("-" * 30)
    preview = await strategic_season_planning(league_id, current_week)
    
    # 2. Trade deadline analysis
    print("\n\n2️⃣ TRADE DEADLINE ANALYSIS")
    print("-" * 30)
    trade_analysis = await optimize_trade_deadline_timing(league_id, current_week)
    
    # 3. Playoff preparation
    print("\n\n3️⃣ PLAYOFF PREPARATION")
    print("-" * 30)
    playoff_prep = await playoff_readiness_assessment(league_id, current_week)
    
    # 4. Season coordination (run less frequently)
    if current_week <= 6:  # Early season - good time for full season planning
        print("\n\n4️⃣ SEASON BYE WEEK COORDINATION")
        print("-" * 30)
        season_plan = await master_bye_week_strategy(league_id, 2025)
    
    print("\n\n✅ STRATEGIC ANALYSIS COMPLETE")
    print("Use these insights to make informed decisions about:")
    print("• When to trade vs when to hold")
    print("• Which weeks to target for waiver claims")
    print("• How to prepare for challenging bye weeks")
    print("• Optimal playoff roster construction")

# Usage - Run this weekly during the season
await full_strategic_analysis("your_league_id_here", current_week=9)
```

## Best Practices for Strategic Sleeper API Usage

### 1. Timing is Everything
- **Early Season (Weeks 1-4)**: Use `get_season_bye_week_coordination()` to plan entire season
- **Mid Season (Weeks 5-10)**: Use `get_strategic_matchup_preview()` weekly for tactical decisions  
- **Trade Season (Weeks 8-13)**: Use `get_trade_deadline_analysis()` to optimize timing
- **Playoff Prep (Weeks 11+)**: Use `get_playoff_preparation_plan()` for readiness assessment

### 2. Strategic Integration
- Combine Sleeper league data with NFL schedule insights
- Use bye week forecasting to plan trades 2-3 weeks early
- Coordinate waiver claims with upcoming matchup difficulties
- Balance short-term needs with playoff preparation

### 3. Competitive Advantage
- Most managers only look 1 week ahead - these tools give you 4-8 week vision
- Early identification of bye week clusters allows proactive roster moves
- Trade deadline timing optimization can secure better deals
- Playoff preparation scoring helps prioritize the right players

### 4. Automation Opportunities
```python
# Set up weekly strategic check
async def weekly_strategic_check():
    """Automated weekly strategic assessment."""
    
    current_week = 9  # Update this weekly or get from NFL state
    league_id = "your_league_id_here"
    
    # Quick strategic pulse check
    preview = await client.call_tool("get_strategic_matchup_preview", {
        "league_id": league_id,
        "current_week": current_week, 
        "weeks_ahead": 3
    })
    
    # Alert on critical upcoming issues
    if preview.data.get("success"):
        summary = preview.data["strategic_preview"]["summary"]
        
        if summary["critical_bye_weeks"]:
            print("🚨 ALERT: Critical bye weeks detected in next 3 weeks!")
            
        if len(summary["challenging_weeks"]) >= 2:
            print("⚠️  WARNING: Multiple challenging weeks ahead - prepare now!")
        
        if summary["trade_recommendations"]:
            print("💡 OPPORTUNITY: Strategic trade window identified!")

# Run this every Tuesday during the season
await weekly_strategic_check()
```