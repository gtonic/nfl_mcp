# Coaching Data Research: Sources and Integration Recommendations

## Overview

This document provides research findings on NFL head coach, offensive coordinator, and defensive coordinator data sources, including:
- Coach records and performance metrics
- Coach-player relationship data
- How this information can be used for performance forecasts
- Recommendations for the 2026 NFL Draft analysis

**Date:** February 4, 2026 (Pre-Super Bowl LX)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Available Data Sources](#available-data-sources)
3. [ESPN API Integration](#espn-api-integration)
4. [Coach-Player Relationship Data](#coach-player-relationship-data)
5. [Performance Forecast Applications](#performance-forecast-applications)
6. [2026 Draft Analysis Integration](#2026-draft-analysis-integration)
7. [Implementation Recommendations](#implementation-recommendations)
8. [Proposed MCP Tools](#proposed-mcp-tools)

---

## Executive Summary

NFL coaching data is critical for fantasy football analysis and draft preparation. Understanding coach-player relationships, coaching schemes, and historical performance can significantly improve player forecasts. This research identifies the best data sources and provides recommendations for integrating coaching intelligence into the NFL MCP Server.

**Key Findings:**
- ESPN Core API provides coaching staff data via team endpoints
- Pro-Football-Reference and Stathead offer comprehensive historical records
- Coach-player relationships require synthesis from multiple sources
- Coaching tree data is available from Pro Football History and Wikipedia
- No single API provides "player development" as a direct metric—this must be derived

---

## Available Data Sources

### 1. ESPN Core API (Recommended - Free)

**Endpoint Structure:**
```
https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{TEAM_ID}/coaches
```

**What It Provides:**
- Head coach name and details
- Coordinators (offensive and defensive)
- Position coaches
- Role descriptions

**How to Access:**
1. Fetch all NFL teams: `https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams?limit=32`
2. For each team, follow the `coaches` link to get coaching staff

**Limitations:**
- Undocumented/unofficial API
- May not include historical coaching data
- Updates may lag at start of season

### 2. Pro-Football-Reference / Stathead

**URL:** https://www.pro-football-reference.com/coaches/

**What It Provides:**
- Complete head coach win-loss records
- Career statistics and tenure data
- Postseason records
- Championship history
- Against-the-spread (ATS) performance

**Access Methods:**
- Manual CSV export via Stathead ($9/month subscription)
- Web scraping (for personal use, respecting TOS)
- Community datasets on GitHub

**GitHub Dataset:**
- Repository: [spatto12/NFLCoaches](https://github.com/spatto12/NFLCoaches/)
- Includes: Season-by-season records, annual win totals, head coach career data

### 3. The Football Database

**URL:** https://www.footballdb.com/coaches/index.html

**What It Provides:**
- Current NFL head coaches with overall win-loss-tie records
- Postseason results
- Championship counts
- Searchable by season

### 4. nfelo (Analytics-Focused)

**URL:** https://www.nfeloapp.com/nfl-head-coaches/

**What It Provides:**
- Career records
- Against-the-spread (ATS) statistics
- Playoff success rates
- Point differential trends

### 5. Pro Football History

**URL:** https://pro-football-history.com/coaches

**What It Provides:**
- Coaching tree visualizations
- Network relationships between coaches
- Historical coaching lineages
- Downloadable data options

### 6. Commercial APIs (Paid)

#### SportsDataIO
- **URL:** https://sportsdata.io/developers/api-documentation/nfl
- **Features:** Complete team rosters, coaching assignments, game data
- **Pricing:** Free trial, paid tiers for advanced data

#### Sportradar
- **URL:** https://developer.sportradar.com/football/docs/nfl-ig-historical-data
- **Features:** Deep team-level and player-level stats (since 2000)
- **Pricing:** Commercial licensing

#### BALLDONTLIE NFL API
- **URL:** https://nfl.balldontlie.io/
- **Features:** OpenAPI-supported, teams/coaches/players endpoints
- **Pricing:** Free tier available

---

## ESPN API Integration

### Recommended Approach for NFL MCP Server

The ESPN Core API is the most practical free option for real-time coaching data. Here's how to integrate it:

**Endpoint Discovery:**
```python
# Step 1: Get all teams
GET https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams?limit=32

# Step 2: For each team, get coaching staff
GET https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{TEAM_ID}/coaches
```

**Data Structure Example:**
```json
{
  "head_coach": {
    "id": "123456",
    "name": "Andy Reid",
    "team": "KC",
    "tenure_years": 12,
    "career_record": {"wins": 273, "losses": 142, "ties": 0}
  },
  "offensive_coordinator": {
    "id": "789012",
    "name": "Matt Nagy",
    "team": "KC"
  },
  "defensive_coordinator": {
    "id": "345678",
    "name": "Steve Spagnuolo",
    "team": "KC"
  }
}
```

---

## Coach-Player Relationship Data

### Sources for Relationship Mapping

1. **Roster + Coaching Staff Cross-Reference**
   - Combine player roster data with coaching staff histories
   - Track which players played under which coaches and for how long

2. **College Connections**
   - Many NFL coaches maintain relationships with college coaches
   - College coach connections can influence draft selections
   - Programs like Ohio State, Alabama, and Georgia have strong NFL pipeline relationships

3. **Coaching Tree Analysis**
   - Coaches from the same "tree" often favor similar player types
   - Andy Reid tree: Harbaugh, Pederson, McDermott, Bowles
   - Kyle Shanahan tree: McDaniel, Saleh
   - Bill Belichick tree: Multiple current head coaches

### Building Coach-Player Familiarity Metrics

**Key Data Points to Track:**
| Metric | Description | Source |
|--------|-------------|--------|
| Years Together | Seasons a player spent under a specific coach | Roster history |
| Position Match | If the coach was a position coach for the player | Coaching staff data |
| Scheme Familiarity | Player experience in similar offensive/defensive schemes | Play-by-play analysis |
| Draft Connection | Coach involved in drafting the player | Transaction history |
| College Pipeline | Coach's known relationships with specific college programs | Historical patterns |

---

## Performance Forecast Applications

### How Coaching Data Improves Forecasts

#### 1. Rookie Development Prediction
- **Metric:** Coach's historical rookie development success rate
- **Application:** Predict which rookies will outperform projections based on coaching quality
- **Data Needed:** Year-over-year player improvement under each coach

#### 2. Scheme Fit Analysis
- **Metric:** Player production in similar schemes
- **Application:** Predict player performance after coaching changes
- **Data Needed:** Scheme classification (West Coast, Air Raid, Gap Scheme, etc.)

#### 3. Positional Hierarchy
- **Metric:** Coordinator's historical target/touch distribution
- **Application:** Predict WR/RB usage patterns and fantasy value
- **Data Needed:** Historical target shares and snap percentages by position

#### 4. Coaching Stability Impact
- **Metric:** Team performance variance during coaching changes
- **Application:** Flag players at risk from coordinator/head coach changes
- **Data Needed:** Performance before/after coaching transitions

### Key Performance Indicators (KPIs)

| KPI | Description | Fantasy Impact |
|-----|-------------|----------------|
| Win Rate | Head coach career win percentage | Team offensive volume |
| Point Differential | Average margin of victory | Game script predictions |
| Red Zone TD% | Offensive/defensive red zone efficiency | Scoring opportunity conversion |
| Time of Possession | Offensive play calling tendencies | Volume predictions |
| Player Development Score | Historical improvement of young players | Rookie valuations |

---

## 2026 Draft Analysis Integration

### Pre-Draft Intelligence

With the 2026 NFL Draft approaching in April, here's how coaching data can enhance draft analysis:

#### 1. Landing Spot Evaluation
- **Factor:** Which teams have coaches known for player development?
- **Data:** Historical rookie performance by coach
- **Outcome:** Adjust dynasty rankings based on landing spot quality

#### 2. Scheme Fit Scoring
- **Factor:** Does the player's college system match the NFL team's scheme?
- **Data:** Scheme classifications for all 32 teams
- **Outcome:** Identify scheme mismatches that may hurt production Year 1

#### 3. Coach-College Connections
- **Factor:** Which coaches have relationships with specific college programs?
- **Data:** Historical draft picks by college, coaching tree connections
- **Outcome:** Predict likely landing spots for top prospects

#### 4. Position Coach Quality
- **Factor:** How successful is the team's position coach with rookies?
- **Data:** Historical Year 1 production for position group
- **Outcome:** Project rookie ceiling/floor more accurately

### Top 2026 Prospects - Coaching Fit Analysis Examples

| Prospect | Position | Key Coaching Factors to Evaluate |
|----------|----------|----------------------------------|
| Caleb Downs (Ohio State) | S | Experience with Saban/Day systems; versatility in coverage schemes |
| Rueben Bain Jr. (Miami) | DE | Fit in 3-4 vs 4-3 systems; pass rush development program |
| Fernando Mendoza (Indiana) | QB | Multiple offensive coordinator experience; scheme adaptability |
| Arvell Reese (Ohio State) | LB | Hybrid defender development; modern defensive schematic fit |

---

## Implementation Recommendations

### Phase 1: Basic Coaching Data (Immediate)

Add new MCP tools to fetch coaching staff from ESPN API:

```python
# New Tool: get_coaching_staff
async def get_coaching_staff(team_id: str) -> dict:
    """
    Get coaching staff for a specific NFL team.
    
    Args:
        team_id: Team abbreviation (e.g., 'KC', 'NE')
        
    Returns:
        Head coach, coordinators, and key position coaches
    """
```

### Phase 2: Historical Records (Short-term)

Integrate Pro-Football-Reference data for coach records:

```python
# New Tool: get_coach_record
async def get_coach_record(coach_name: str) -> dict:
    """
    Get historical win-loss record for a coach.
    
    Args:
        coach_name: Coach's full name
        
    Returns:
        Career record, postseason record, tenure history
    """
```

### Phase 3: Coaching Tree Analysis (Medium-term)

Build coaching tree relationships for scheme analysis:

```python
# New Tool: get_coaching_tree
async def get_coaching_tree(coach_name: str) -> dict:
    """
    Get coaching tree connections for a coach.
    
    Args:
        coach_name: Coach's full name
        
    Returns:
        Mentors, proteges, and scheme family
    """
```

### Phase 4: Player Development Analytics (Long-term)

Create derived metrics for player development:

```python
# New Tool: get_coach_development_score
async def get_coach_development_score(coach_name: str, position: str) -> dict:
    """
    Calculate coach's historical success developing players at a position.
    
    Args:
        coach_name: Coach's full name
        position: Player position (QB, WR, RB, etc.)
        
    Returns:
        Development score, historical examples, trend
    """
```

---

## Proposed MCP Tools

### Tool Registry Additions

```python
# Coaching Intelligence Tools
get_coaching_staff,          # Team coaching staff from ESPN
get_coach_record,            # Historical win-loss records
get_coaching_tree,           # Coach relationships and lineage
get_scheme_classification,   # Team offensive/defensive scheme type
get_coach_development_score, # Player development analytics
get_coach_player_history,    # Which players worked under which coaches
get_coordinator_tendencies,  # Play-calling patterns and tendencies
```

### Integration with Existing Tools

| Existing Tool | Coaching Enhancement |
|--------------|---------------------|
| `get_depth_chart` | Add coach responsible for each position group |
| `analyze_trade` | Factor in coaching quality at destination |
| `get_strategic_matchup_preview` | Include scheme matchup analysis |
| `get_roster_recommendations` | Consider coaching scheme fit |

---

## Data Source Comparison Matrix

| Source | Cost | API? | Real-time? | Historical? | Coach Records | Relationships |
|--------|------|------|------------|-------------|---------------|---------------|
| ESPN Core API | Free | Yes | Yes | Limited | Partial | No |
| Pro-Football-Reference | Free/$9/mo | No | Manual | Excellent | Full | Partial |
| SportsDataIO | Paid | Yes | Yes | Good | Full | No |
| Sportradar | Paid | Yes | Yes | Excellent | Full | No |
| Pro Football History | Free | No | Manual | Good | Full | Excellent |
| Wikipedia | Free | No | Manual | Excellent | Full | Good |

---

## Conclusion

Integrating coaching data into the NFL MCP Server will significantly enhance fantasy football analysis capabilities, particularly for:

1. **Rookie evaluation** - Predict which rookies will succeed based on landing spot
2. **Coaching change impact** - Quickly assess how staff changes affect player values
3. **Draft preparation** - Improve 2026 draft target identification
4. **Trade analysis** - Better evaluate destination team quality

The recommended implementation path starts with ESPN API integration for real-time coaching staff data, followed by historical records from Pro-Football-Reference, and eventually building derived player development metrics.

---

## References

1. ESPN Core API - https://sports.core.api.espn.com
2. Pro-Football-Reference Coaches - https://www.pro-football-reference.com/coaches/
3. Stathead Football - https://stathead.com/football/
4. The Football Database - https://www.footballdb.com/coaches/index.html
5. nfelo Head Coaches - https://www.nfeloapp.com/nfl-head-coaches/
6. Pro Football History Coaching Trees - https://pro-football-history.com/blog/10/98/current-nfl-coaching-trees
7. SportsDataIO NFL API - https://sportsdata.io/developers/api-documentation/nfl
8. Sportradar NFL Historical Data - https://developer.sportradar.com/football/docs/nfl-ig-historical-data
9. BALLDONTLIE NFL API - https://nfl.balldontlie.io/
10. GitHub NFLCoaches Dataset - https://github.com/spatto12/NFLCoaches/
11. Gridiron Experts Coach List - https://gridironexperts.com/nfl-coaches-list/

---

*Document created: February 4, 2026*
*NFL MCP Server Research Initiative*
