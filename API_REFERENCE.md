# API Quick Reference

## When to Use Which Tool

### User Wants NFL News/Information
- **"NFL news"**, **"recent stories"** → `get_nfl_news(limit=10)`
- **"team list"**, **"NFL teams"** → `get_teams()`
- **"Chiefs starters"**, **"depth chart"** → `get_depth_chart(team_id="KC")`
- **"passing leaders"**, **"sack leaders"** → `get_league_leaders(category="pass")`

### User Mentions Player Names
- **"find Mahomes"**, **"search Josh Allen"** → `search_athletes(name="Mahomes")`
- **"Chiefs roster"**, **"who plays for KC"** → `get_athletes_by_team(team_id="KC")`
- **Player ID provided** → `lookup_athlete(athlete_id="1234")`

### User Provides URLs
- **"analyze this URL"**, **"what does this page say"** → `crawl_url(url="https://...")`

### User Mentions Fantasy League
- **League ID provided** → `get_league(league_id="123456789012345678")`
- **"my team roster"**, **"league rosters"** → `get_rosters(league_id="...")`
- **"week 5 matchup"**, **"this week's games"** → `get_matchups(league_id="...", week=5)`
- **"league members"** → `get_league_users(league_id="...")`
- **"playoff bracket"** → `get_playoff_bracket(league_id="...")`
- **"waiver wire"**, **"trending players"** → `get_trending_players(trend_type="add")`
- **"what week is it"** → `get_nfl_state()`

### Maintenance/Setup
- **Before player searches** → `fetch_athletes()` (updates database)
- **Before team queries** → `fetch_teams()` (updates database)

## Common Parameter Patterns

### Team IDs
Use 2-3 letter abbreviations: `"KC"`, `"SF"`, `"NE"`, `"DAL"`, `"GB"`

### League IDs  
18-digit numeric strings from Sleeper: `"123456789012345678"`

### Limits
- News: 1-50 articles (default: 50)
- Player search: 1-100 results (default: 10)  
- Trending players: 1-100 results (default: 25)

### Statistical Categories
- `"pass"` - passing stats
- `"rush"` - rushing stats  
- `"receiving"` - receiving stats
- `"tackles"` - defensive tackles
- `"sacks"` - quarterback sacks
- Combine with commas: `"pass, rush"`