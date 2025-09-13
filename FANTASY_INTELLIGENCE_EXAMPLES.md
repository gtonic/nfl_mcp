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

## Advanced Decision Making Workflows

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