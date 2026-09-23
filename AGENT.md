# NFL MCP Server - AI/LLM Integration Guide

## Overview

The NFL MCP (Model Context Protocol) Server is a specialized FastMCP server designed to provide AI/LLM systems with comprehensive access to NFL and fantasy football data. This guide explains the MCP functionality, tool organization, and best practices for AI/LLM integration.

## What is MCP?

The Model Context Protocol (MCP) is a standardized protocol that allows AI/LLM systems to interact with external tools and data sources in a structured, type-safe manner. This server implements MCP to expose NFL data through a consistent interface that AI systems can understand and utilize.

## Architecture

### Server Design

The NFL MCP Server follows a simplified, maintainable architecture:

- **Single Tool Registry**: All tools defined in one centralized location (`tool_registry.py`)
- **FastMCP Framework**: Built on FastMCP 3.0+ for robust MCP protocol support
- **HTTP Transport**: Runs on HTTP transport (default port 9000)
- **Database Caching**: SQLite-based persistence for optimal performance
- **Async/Await**: Fully asynchronous for efficient concurrent operations

### Core Components

1. **Tool Registry** (`tool_registry.py`): Central registration of all MCP tools
2. **NFL Tools** (`nfl_tools.py`): ESPN API integration for NFL data
3. **Sleeper Tools** (`sleeper_tools.py`): Fantasy league management via Sleeper API
4. **Athlete Tools** (`athlete_tools.py`): Player data management and caching
5. **Web Tools** (`web_tools.py`): URL crawling and content extraction
6. **Waiver Tools** (`waiver_tools.py`): Advanced waiver wire analysis
7. **Trade Analyzer** (`trade_analyzer_tools.py`): Trade evaluation and analysis

## Tool Categories

The server has **72 MCP tools** (including `get_league_leaders`, behind the
`league_leaders` feature flag, enabled by default). Which of them are registered
depends on the tool profile, `NFL_MCP_TOOL_PROFILE`:

| Profile | Tools | Registered |
|---|---|---|
| `season` (default) | 55 | everything except draft (8), coaching (4), admin cache refreshes (`fetch_athletes`, `fetch_all_players`, `fetch_teams`), `get_league_leaders`, `get_cbs_expert_picks` |
| `offseason` | 43 | draft and coaching; not the in-season-only tools (briefing, retro, league changes, bye plan, lineups/start-sit, waivers/FAAB/IR, Vegas, weather, streaming, matchups, opponent, playoff odds/bracket, trade finder, usage/opportunity), admin or `get_cbs_expert_picks` |
| `full` | 72 | everything |

The profile and count are logged at startup and returned by `GET /health`
under `tools`. Every tool also ships its own parameter schema over MCP, so an
agent can introspect the authoritative signature at runtime; this list is the
map. Each tool answers one distinct question — earlier overlapping tools are
now parameters of the one kept (see the CHANGELOG for the old → new mapping).

### 1. NFL Information Tools (8 tools)

Core NFL data access for teams, news, standings, and schedules:

- **`get_nfl_news`**: Latest NFL news from ESPN API
  - Parameters: `limit` (optional, default 50, max 50)
  - Use case: Get current NFL headlines and stories
  - Returns: Articles with headlines, descriptions, published dates

- **`get_teams`**: All NFL team information
  - Parameters: None
  - Use case: Retrieve complete list of NFL teams
  - Returns: Team data including names, abbreviations, IDs

- **`fetch_teams`**: Cache teams in local database
  - Parameters: None
  - Use case: Initialize or refresh team data cache
  - Returns: Count of teams processed and timestamp

- **`get_depth_chart`**: Team roster/depth chart
  - Parameters: `team_id` (required, e.g., "KC", "NE")
  - Use case: Analyze team composition and player depth
  - Returns: Positions with players in depth order

- **`get_team_player_stats`**: Team player statistics
  - Parameters: `team_id` (required), `season`, `season_type`, `limit`
  - Use case: Evaluate player performance and fantasy relevance
  - Returns: Player stats with fantasy relevance indicators

- **`get_nfl_standings`**: Current NFL standings
  - Parameters: `season`, `season_type`, `group` (optional)
  - Use case: Understand playoff implications and team motivation
  - Returns: Standings with fantasy context and motivation levels

- **`get_team_schedule`**: Team schedules with fantasy context
  - Parameters: `team_id` (required), `season` (optional)
  - Use case: Strength of schedule and matchup analysis
  - Returns: Games with fantasy implications and matchup details

- **`get_league_leaders`**: NFL statistical leaders by category
  - Parameters: Category-specific parameters
  - Use case: Identify top performers across the league
  - Returns: Leader boards for various statistical categories

### 2. Coaching Intelligence Tools (4 tools)

Coaching staff information, coaching trees, and scheme analysis:

- **`get_coaching_staff`**: Get coaching staff for a specific NFL team
  - Parameters: `team_id` (required, e.g., "KC", "NE")
  - Use case: Identify head coach, coordinators, and position coaches
  - Returns: Head coach, offensive/defensive coordinators, all coaches with roles

- **`get_all_coaching_staffs`**: Get coaching staff summary for all 32 NFL teams
  - Parameters: None
  - Use case: Quick overview of all head coaches across the league
  - Returns: List of teams with head coach names and coach counts

- **`get_coaching_tree`**: Get coaching tree information for a coach
  - Parameters: `coach_name` (required, e.g., "Andy Reid", "Bill Belichick")
  - Use case: Understand coaching lineage, mentors, and proteges
  - Returns: Mentors, proteges, scheme family, what coach is known for, `as_of`
  - Available coaches: Andy Reid, Bill Belichick, Kyle Shanahan, Sean McVay, Mike Tomlin, Sean Payton
  - **This is lineage history, not current employment** — an entry says who a
    coach learned under, not where he works. `found: false` means "not in this
    curated list", not "has no lineage"

- **`get_scheme_classification`**: Get offensive/defensive scheme for a team,
  resolved from its **current** coaching staff
  - Parameters: `team_id` (required, e.g., "SF", "KC"), `season` (optional),
    `use_live_staff` (optional, default True — False skips the network call)
  - Use case: Analyze scheme fit for players, understand play-calling tendencies
  - Returns: offensive/defensive scheme, `offense`/`defense` provenance
    (`{scheme, source, attributed_to, role}`), head_coach, scheme_notes,
    `is_fallback`, `as_of`, `warnings`
  - **Check `source` before trusting it.** `coach` means the scheme was read off
    the named, live-fetched coordinator or head coach. `team_table` means no
    scheme is on file for that coach and the answer is a dated team-level guess
    — possibly a regime out of date; `warnings` names who could not be matched

  A scheme belongs to the play-caller, not the franchise, which is why this is
  keyed on the coach: roughly a quarter of the league changes coordinator every
  offseason, and a per-team table silently becomes last regime's answer.

### 3. Player/Athlete Tools (4 tools)

Player data management with Sleeper API integration:

- **`fetch_athletes`**: Import all NFL players (expensive operation)
  - Parameters: None
  - Use case: Initialize comprehensive player database
  - Warning: Large data operation, use sparingly

- **`lookup_athlete`**: Find player by ID
  - Parameters: `athlete_id` (required)
  - Use case: Get specific player information
  - Returns: Detailed athlete data

- **`search_athletes`**: Search players by name
  - Parameters: `name` (required), `limit` (optional, default 10, max 100)
  - Use case: Find players with partial name matching
  - Returns: Matching athletes with positions and teams

- **`get_athletes_by_team`**: Get team roster
  - Parameters: `team_id` (required, e.g., "KC")
  - Use case: Analyze team composition
  - Returns: All athletes on specified team

### 4. Web Scraping Tools (1 tool)

Generic URL content extraction:

- **`crawl_url`**: Extract text from any webpage
  - Parameters: `url` (required), `max_length` (optional, default 10000)
  - Use case: Scrape NFL-related content from any website
  - Returns: Cleaned text content optimized for LLM consumption
  - Security: Validates URLs, removes scripts, sanitizes content

### 5. Fantasy League Tools - Sleeper API (17 tools)

Comprehensive fantasy football league management:

#### Core League Tools
- **`get_league`**: League information and settings
- **`get_rosters`**: All team rosters with player enrichment
- **`get_league_users`**: League members/managers
- **`get_user`**: Individual user profile
- **`get_user_leagues`**: All leagues for a user

#### Matchup & Competition Tools
- **`get_matchups`**: Weekly matchups with enriched player data
- **`get_playoff_bracket`**: Playoff bracket (supports winners/losers)
- **`get_fantasy_context`**: Aggregated league data in one call

#### Transaction & Activity Tools
- **`get_transactions`**: League transactions by week
- **`get_traded_picks`**: Draft pick trades

#### Draft Tools
- **`get_league_drafts`**: All drafts for a league
- **`get_draft`**: Specific draft information
- **`get_draft_picks`**: All picks in a draft
- **`get_draft_traded_picks`**: Traded draft picks

#### Global NFL & Trends
- **`get_nfl_state`**: Current NFL week and season state
- **`get_trending_players`**: Players trending in add/drop activity
- **`fetch_all_players`**: Complete player dataset (cached)

### 6. Bye-Week Planning (1 tool)

- **`get_bye_week_plan`**: which upcoming weeks YOUR lineup runs short, and what to add
  - Parameters: `league_id` (required), `roster_id` or `user_id`, `weeks_ahead`
    (optional, default 6), `include_free_agents` (optional, default True), `week`, `season`
  - For each week: starters/bench on bye or injured out, the best legal lineup
    total in the league's slots and scoring (ROS weekly projections), empty
    slots, the bye cost against full strength, status ok/thin/crunch; crunch
    weeks get a suggestion ("Week 7: only 1 RB available for 2 RB slot(s) — add
    a RB before week 7") and free agents for the two worst weeks
    (`get_waiver_targets` for that week, 25 s timeout). Includes trade-deadline
    status; trade proposals are `find_trade_targets`.
  - Returns: weeks, crunch_weeks, thin_weeks, suggestions, free_agent_options,
    core_starters, trade_deadline, method

### 7. Waiver Wire Analysis Tools (2 tools)

Advanced waiver wire intelligence:

- **`get_waiver_targets`**: **START HERE for "who should I pick up"** — ranks the
  players nobody in *your* league rosters by how much they would upgrade *your*
  lineup
  - Parameters: `league_id` (required), `roster_id` or `user_id`, `week`,
    `season`, `positions` (optional), `limit` (optional, default 12)
  - Use case: the weekly waiver claim. Prefer this over `get_trending_players`,
    which reports league-agnostic add counts and includes players already taken
  - Returns: targets (with projected_points, floor/ceiling, replacement_level,
    upgrade_points, trending_adds, verdict), drop_candidates,
    replacement_levels, thin_positions, waiver_type, pool_size

- **`get_waiver_log`**: what already happened on the league's waiver wire
  - Parameters: `league_id`, `round` (optional, default current week),
    `sections` (optional: any of "log", "summary", "re_entries"; default all),
    `player` (optional Sleeper id or name filter), `dedupe` (optional, default true)
  - Returns: waiver_log + failed_claims (log), dashboard_summary (summary),
    re_entry_players + volatile_players (re_entries). Pending claims are never
    visible — Sleeper exposes a claim only once processed.

### 8. Trade Analysis Tools (2 tools)

Trade evaluation and discovery:

- **`find_trade_targets`**: **START HERE for "who should I trade with"** — finds
  the deal rather than grading one you already have
  - Parameters: `league_id` (required), `roster_id` or `user_id`, `week`,
    `season`, `positions` (optional), `limit` (optional, default 10)
  - Use case: the trade question. Scores every one-for-one swap against every
    other roster by recomputing **both** teams' best legal starting lineup
    before and after, and returns only trades where both sides gain — a trade
    the other manager loses is a wish, not a deal
  - `horizon`: `"ros"` (default) scores both lineups over every remaining
    week (byes and injury absences included, see `get_ros_projections`);
    `"week"` scores this week only. The league's `trade_deadline` is read:
    past it no proposals are returned, within a week it is flagged urgent
  - Returns: proposals (you_give, you_get, your_gain, their_gain, mutual_gain,
    partner), trade_deadline, your_replacement_levels, candidates_considered
    (every swap scored), caveats

- **`get_ros_projections`**: rest-of-season and fantasy-playoff points in the
  league's scoring — use instead of `project_players` for trades, drops and
  stashes
  - Parameters: `league_id` (required), `roster_id` / `player_ids` /
    `player_names`, `season`, `week`, `include_weekly`
  - Returns: per player ros_points, playoff_points, total_points,
    weeks_counted, bye_weeks, injury_weeks, injury_window, per_game
  - **Gains are this week's lineup points, not rest-of-season value.** Run the
    chosen deal through `analyze_trade` before sending it

- **`analyze_trade`**: Evaluate a specific proposed trade
  - Parameters: Trade-specific parameters
  - Use case: Assess fairness and market value of a deal already on the table
  - Returns: Trade analysis with recommendations

### 9. Draft & Player Values (4 tools)

- **`get_draft_board`**: Build a tiered, VBD-ranked draft board (the ordering that wins drafts).
  - Parameters: `scoring` (optional, default 'ppr'), `superflex` (optional, default False), `num_teams` (optional, default 12), `dynasty` (optional, default False), `position` (optional), `limit` (optional, default 60)
  - Returns: board, tiers_by_position, replacement_values, format, source, stale
- **`recommend_draft_pick`**: Recommend the best pick(s) right now in a live Sleeper draft.
  - Parameters: `draft_id` (required), `my_slot` (optional), `num_suggestions` (optional, default 5)
  - Returns: suggestions, top_pick, best_available_by_position, value_cliffs, positional_run, my_roster, format, source
- **`simulate_draft`**: Rehearse a full snake draft offline (solo, repeatable).
  - Parameters: `my_slot` (required), `num_teams` (optional, default 12), `rounds` (optional, default 15), `scoring` (optional, default 'ppr'), `superflex` (optional, default False), `dynasty` (optional, default False), `randomness` (optional, default 0.35), `num_sims` (optional, default 1), `seed` (optional)
  - Returns: sample, my_team, standings, grade
- **`get_player_values`**: Consensus player market values (real values, not heuristics): the best-first list, or specific players.
  - Parameters: `players` (optional list of Sleeper ids or names — one works), `scoring` (optional, default 'ppr'), `superflex` (optional, default False), `num_teams` (optional, default 12), `dynasty` (optional, default False), `position` (optional), `limit` (optional, default 100)
  - Returns: values, total, not_found (lookups), format, source, stale, updated_at

### 10. Weekly Projections (7 tools)

**Scoring matters and is honoured.** `scoring` sets the points scale, not just
which market values are consulted: both baselines are rebased to the league's
per-reception value, so a half-PPR league gets half-PPR points *and* the
receiver-vs-runner ordering that follows. `get_weekly_briefing` reads the exact
value out of the league's own `scoring_settings`; pass `scoring` yourself for the
lower-level tools (`'ppr'`, `'half_ppr'`, `'standard'`, or a raw value like
`'0.5'`).

**Floor and ceiling are calibrated**, not decorative: they are a ±1σ band that
covers the real outcome ~68% of the time, measured in
`evals/backtest/calibration.py`. Treat the floor as a real floor.

- **`get_weekly_briefing`**: One call for "how should I line up this week". Mid-week, players whose game has kicked off carry their actual points with no remaining variance and their slots leave the optimization.
  - Parameters: `league_id` (required), `roster_id` (optional), `user_id` (optional), `week` (optional), `season` (optional)
  - Returns: league, week, record, win_probability, projected_points, opponent_projected_points, recommended_lineup, changes, bench, injury_changes, not_projected

- **`get_weekly_retro`**: Post-game review of a finished week (default: the last completed one). Each starter's actual points against the projection logged before kickoff, points left on the bench (exact hindsight lineup under the league's slot rules), result vs the opponent and whether the hindsight lineup would have flipped it, biggest misses/hits, and projection calibration over every logged week. A week with no logged projection is re-projected and labelled `projection_source: "recomputed"`.
  - Parameters: `league_id` (required), `roster_id` (optional), `user_id` (optional), `week` (optional), `season` (optional), `include_calibration` (optional, default True)
  - Returns: result, projected_total, projection_source, starters, bench, hindsight, biggest_misses, biggest_hits, opponent, calibration

- **`get_league_changes`**: The daily delta for one roster since its last check (remembered per league and roster): injury moves on your roster and the opponent's starters, news naming them, league transactions, trending backups of your starters, and projection moves on your starters. Ranked by importance.
  - Parameters: `league_id` (required), `roster_id` (optional), `user_id` (optional), `since` (optional ISO-8601), `mark_seen` (optional, default True), `projection_threshold` (optional, default 2.0), `limit` (optional, default 25)
  - Returns: changes [{kind, importance, summary, ...}], counts, omitted, since, since_source, checked_at, errors

- **`project_players`**: This week's fantasy points for one or more players (a one-element list for one).
  - Parameters: `players` (required; dicts with name, position, team, optional opponent, player_id, usage, injury, weather {wind_mph, is_dome}), `league_id` (optional), `scoring` (optional, default 'ppr'), `superflex` (optional, default False), `num_teams` (optional, default 12), `season` (optional), `week` (optional)
  - Returns: projections, total
- **`get_opportunity_projections`**: Opportunity-based projections from trailing volume (beats trailing-PPG).
  - Parameters: `season` (required), `week` (required), `players` (optional), `lookback` (optional, default 6), `min_games` (optional, default 2), `top_n` (optional, default 50), `scoring` (optional, default 'ppr')
  - Returns: season, week, lookback, scoring, ppr, count, projections, player_id, name, position
- **`get_usage_trends`**: Week-by-week usage for a roster or named players — target share, air-yards share, WOPR, RACR, carries share (nflverse), snap share and red-zone opportunities (Sleeper's counted `rec_rz_tgt` + `rush_rz_att`) — with a rising/falling/stable direction per metric and a short flag list.
  - Parameters: `league_id` + `roster_id` (optional), `player_names` (optional), `weeks` (optional, default 4), `season` (optional), `through_week` (optional)
  - Returns: season, window, players (weeks, trends, flags), sources, trend_method, notes

`project_players` also carries Sleeper's own weekly projection, priced in the league's scoring, as a second opinion: `sleeper_projection`, `consensus` (plain average) and `disagreement` (> 4 pts, or > 25% and >= 2 pts). Our `projected_points` stays the number decisions are made on.

### 11. Start/Sit & Lineup Optimization (4 tools)

- **`get_start_sit_recommendation`**: Start or sit? One player (`player_name`) or several (`players`, names or partial dicts). Team, position, Sleeper id, opponent, last week's snap share, injury designation and practice report are looked up server-side (QB/RB/WR/TE and K/DEF).
  - Parameters: `player_name` or `players`, `position`/`team`/`player_id` (optional, to disambiguate), `opponent` (optional), `injury_status` (optional override), `league_id` (preferred, league scoring), `scoring`, `season`, `week`, `include_reasoning`
  - Returns (single): recommendation, confidence, matchup_tier, reasoning, factors, resolved; (list): recommendations, by_position, must_starts, sits, on_bye, locked
- **`compare_players_for_slot`**: Compare 2-5 players competing for the same roster slot (names or dicts; details looked up).
  - Parameters: `players` (required), `slot` (optional, default 'FLEX'), `league_id` (optional)
  - Returns: winner, comparison, confidence_gap, verdict
- **`analyze_lineup`**: Grade the lineup you have set this week, read from the league.
  - Parameters: `league_id` + `roster_id` or `user_id`, `week`, `season`; or `lineup` (dict keyed by slot) for a hypothetical
  - Returns: lineup_grade, lineup_efficiency_pct, total_projected, optimal_projected, optimal_lineup, suggested_changes, locked_players, weak_spots, starters, bench, empty_slots
- **`get_win_probability_lineup`**: Pick the lineup that maximizes P(beating this specific opponent).
  - Parameters: `your_players` (required), `opponent_players` (required), `slots` (optional), `stack_correlation` (optional, default 0.35)
  - Returns: recommended_lineup, win_probability, projected_points, opponent_projected_points, projected_margin, you_are, strategy, points_optimal_lineup

### 12. Matchup, Schedule & Weather (5 tools)

- **`get_defense_rankings`**: NFL defenses ranked by fantasy points allowed per position, or one defense via `opponent_team`.
  - Parameters: `positions` (optional), `season` (optional), `opponent_team` (optional)
  - Returns: rankings, matchups {position: {rank, matchup_tier, recommendation}} (with opponent_team), positions, season, is_fallback, tiers_explained
- **`analyze_roster_matchups`**: This week's defensive matchup for every player on your roster.
  - Parameters: `league_id` + `roster_id` or `user_id` (roster and opponents loaded), or `players` [{name, position, opponent}]; `week`, `season`
  - Returns: analysis, smash_spots, avoid_spots, summary, on_bye, total_analyzed
- **`get_strength_of_schedule`**: Rank NFL teams by schedule difficulty over a week range, per position.
  - Parameters: `season`, `start_week`, `end_week` (all optional: current season/week to 17), `positions` (optional), `strength_season` (optional), `playoff_weeks` (optional bool), `league_id` (optional; its playoff window with playoff_weeks, else weeks 15-17)
  - Returns: season, weeks, window, positions, strength_source_season, strength_is_fallback, by_position, overall
- **`get_streaming_options`**: Rank weekly streaming options per position over the next 1-4 weeks.
  - Parameters: `season` (required), `start_week` (required), `weeks_ahead` (optional, default 3), `positions` (optional), `strength_season` (optional), `top_n` (optional, default 8), `league_id` (optional), `only_available` (optional, default False)
  - Returns: season, weeks, positions, defense_source_season, defense_is_fallback, offense_source_season, offense_is_fallback, availability_active
- **`get_weather_forecast`**: Per-game weather forecast + fantasy impact for an NFL week (Open-Meteo, no key).
  - Parameters: `season` (required), `week` (required), `teams` (optional)
  - Returns: season, week, count, games, home, away, kickoff, stadium

### 13. Vegas Lines & Game Environment (2 tools)

- **`get_vegas_lines`**: Spreads, totals, implied team totals — per game, per team, or for your roster.
  - Parameters: `teams` (optional; adds team_environments), `week`, `season`, `league_id` + `roster_id`/`user_id` (optional; adds the per-player roster view)
  - Returns: games, summary, team_environments?, roster? {analysis, best_environments, worst_environments, on_bye}
- **`get_stack_opportunities`**: Identify high-total games for stacking opportunities.
  - Parameters: `min_total` (optional, default 48.0)

### 14. Injury Intelligence (3 tools)

- **`get_injury_report`**: Who is hurt now (ESPN reports, cached), with this week's practice line.
  - Parameters: `teams` (optional), `player_ids` (optional), `min_confidence`, `severity` (min 1-5), `since` (ISO), `include_practice` (default True), `limit`, `use_cache`; `team_ids` is a deprecated alias of `teams`
  - Only ESPN is wired in (the CBS injury source is not implemented), so `confidence` is uniform today.
  - Returns: injuries, total_injuries, filters, practice_week
- **`get_injury_trends`**: Get injury status CHANGES over a window - who got worse or recovered.
  - Parameters: `lookback_hours` (optional, default 168), `teams` (optional), `direction` (optional), `limit` (optional, default 50)
  - Returns: changes, player_name, team_id, position, previous_status, injury_status, direction, severity_delta
- **`get_gameday_inactives`**: Get likely inactive players for upcoming games.
  - Parameters: `teams` (optional), `severity_threshold` (optional, default 3)
  - Returns: inactives, player_name, team_id, injury_status, severity

### 15. Opponent & Season Strategy (2 tools)

- **`analyze_opponent`**: Analyze an opponent's roster to identify weaknesses and exploitation opportunities.
  - Parameters: `league_id` (required), `opponent_roster_id` (required), `current_week` (optional)
  - Returns: vulnerability_score, vulnerability_level, high, moderate, position_assessments
- **`get_playoff_odds`**: Compute playoff probabilities via Monte-Carlo of the rest of the season.
  - Parameters: `league_id` (required), `current_week` (optional), `num_sims` (optional, default 10000), `score_sd` (optional, default 25.0), `my_roster_id` (optional), `seed` (optional)
  - Returns: odds, roster_id, name, record, mean_ppg, playoff_pct

### 16. FAAB & Handcuffs (2 tools)

- **`recommend_faab_bid`**: Recommend a FAAB waiver bid for a player (% of budget + absolute).
  - Parameters: `league_id` (required), `player_id` (optional), `player_name` (optional), `my_roster_id` (optional)
  - Returns: recommendation, bid_pct, bid_absolute, tier, range, reasoning
- **`get_handcuff_map`**: Map each of your RB starters to its handcuff + the handcuff's availability.
  - Parameters: `league_id` (required), `roster_id` (required)
  - Returns: handcuffs, starter, team, handcuff, handcuff_status, handcuff_player_id

### 17. CBS Sports (3 tools)

- **`get_cbs_player_news`**: Fetch latest fantasy football player news from CBS Sports.
  - Parameters: `limit` (optional, default 50)
  - Returns: news, total_news, success
- **`get_cbs_projections`**: CBS SEASON-LONG projections for a position (CBS ignores weeks, so there is no week parameter).
  - Parameters: `position` (optional, default 'QB'), `season` (optional, default 2026), `scoring` (optional, default 'ppr')
  - Returns: projections, total_projections, period "season", week_honoured false, position, success
- **`get_cbs_expert_picks`** (`full` profile): CBS experts' NFL picks against the spread — betting picks, not fantasy advice.
  - Parameters: `week` (optional, default current)
  - Returns: picks, total_picks, week, success

## Advanced Features

### Data Enrichment

The server provides multiple levels of data enrichment to enhance fantasy decision-making:

#### Basic Enrichment (Always Available)
- Player name resolution from IDs
- Team assignment and abbreviations
- Position information
- Basic statistics

#### Advanced Enrichment (NFL_MCP_ADVANCED_ENRICH=1)
- **Snap Percentages**: Offensive snap % for current week
  - `snap_pct`: Actual or estimated snap percentage
  - `snap_pct_source`: "cached" or "estimated"
  
- **Opponent Information**: Next opponent for defense units
  - `opponent`: Team abbreviation
  - `opponent_source`: "cached" or "fetched"

- **Practice Status**: Latest injury practice designation
  - `practice_status`: DNP, LP, FP, Full
  - `practice_status_date`: Date of practice report
  - `practice_status_age_hours`: Age of report
  - `practice_status_stale`: Boolean if older than 72h

- **Usage Metrics** (WR/RB/TE only): 3-week rolling averages
  - `targets_avg`: Average targets per game
  - `routes_avg`: Average routes run per game
  - `rz_touches_avg`: Average red zone touches per game
  - `snap_share_avg`: Average snap share percentage
  - `weeks_sample`: Number of weeks in sample (1-3)

- **Usage Trends**: Directional trend analysis
  - `targets`: Trend for targets (up/down/flat)
  - `routes`: Trend for routes (up/down/flat)
  - `snap_share`: Trend for snap percentage (up/down/flat)
  - `usage_trend_overall`: Overall trend direction

### Prefetch & Caching

Background data prefetching for optimal performance:

- **Enable**: set **both** `NFL_MCP_PREFETCH=1` *and* `NFL_MCP_ADVANCED_ENRICH=1`.
  `NFL_MCP_PREFETCH` starts the loop; `NFL_MCP_ADVANCED_ENRICH` ungates the
  individual fetchers. With only the first set, the loop starts and immediately
  returns, and the usage/snap/injury tables stay empty.
- **Interval**: `NFL_MCP_PREFETCH_INTERVAL` (default: 900 seconds = 15 min)
- **Snap TTL**: `NFL_MCP_PREFETCH_SNAPS_TTL` (default: 900 seconds)
- **Athletes refresh**: `NFL_MCP_PREFETCH_ATHLETES` (default: on) every `NFL_MCP_PREFETCH_ATHLETES_INTERVAL` (default: 86400 seconds = daily)

The prefetch system automatically:
1. Determines current season/week via NFL state
2. Fetches team schedules (caches opponent data)
3. Fetches player snap counts (`player_week_stats`) and weekly usage —
   targets, touches, red-zone touches, air yards, snap share (`player_usage_stats`)
4. Fetches injury reports, recording every status change in `injury_history`
5. Fetches practice reports (Thursday–Saturday only)
6. Refreshes the athletes cache (player names/teams/positions) at startup and daily
7. Refreshes on configured intervals

Each cycle fetches only the **previous** week, so a server started mid-season
never acquires the earlier ones. Use `scripts/backfill_usage.py` to fill a week
range after the fact:

```bash
python -m scripts.backfill_usage --season 2026 --through 5
```

### Robustness & Resilience

Many endpoints implement retry logic with snapshot fallback:

- **Retry Strategy**: Multiple attempts with exponential backoff
- **Snapshot Fallback**: Returns stale but usable data on failure
- **Metadata Fields**:
  - `retries_used`: Number of retry attempts
  - `stale`: Boolean indicating stale data
  - `failure_reason`: Last error code/category
  - `snapshot_fetched_at`: Timestamp of cached data
  - `snapshot_age_seconds`: Age of snapshot

Example response with snapshot:
```json
{
  "success": false,
  "stale": true,
  "retries_used": 3,
  "failure_reason": "timeout",
  "snapshot_fetched_at": "2025-09-13T11:22:33Z",
  "snapshot_age_seconds": 642,
  "data": { ... }
}
```

## Configuration

### Environment Variables

The server supports extensive configuration via environment variables:

#### Core Settings
- `NFL_MCP_TIMEOUT_TOTAL`: Total request timeout (default: 30.0 seconds)
- `NFL_MCP_TIMEOUT_CONNECT`: Connection timeout (default: 10.0 seconds)
- `NFL_MCP_NFL_NEWS_MAX`: Max news articles (default: 50)
- `NFL_MCP_SERVER_VERSION`: Server version string

#### Advanced Features
- `NFL_MCP_ADVANCED_ENRICH`: Enable advanced enrichment (0 or 1). **Also required
  for the prefetch loop** — without it the loop exits immediately.
- `NFL_MCP_PREFETCH`: Enable background prefetch (0 or 1). Needs
  `NFL_MCP_ADVANCED_ENRICH=1` alongside it to do anything.
- `NFL_MCP_PREFETCH_INTERVAL`: Prefetch interval in seconds (default: 900)
- `NFL_MCP_PREFETCH_SNAPS_TTL`: Snap data TTL in seconds (default: 900)
- `NFL_MCP_PREFETCH_ATHLETES`: Refresh athletes cache during prefetch (0 or 1, default: 1)
- `NFL_MCP_PREFETCH_ATHLETES_INTERVAL`: Athletes refresh interval in seconds (default: 86400)

#### Logging
- `NFL_MCP_LOG_LEVEL`: Log verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL)

### Configuration Files

Alternative to environment variables, use YAML or JSON config files:

```yaml
timeout:
  total: 45.0
  connect: 15.0

limits:
  nfl_news_max: 75
  athletes_search_max: 150

rate_limits:
  default_requests_per_minute: 120

security:
  max_string_length: 2000
```

## Security Considerations

The server implements comprehensive security measures:

### Input Validation
- SQL injection pattern detection
- XSS injection pattern detection
- Command injection pattern detection
- Path traversal pattern detection
- Type checking and range validation

### Content Security
- HTML sanitization with script removal
- URL validation (HTTP/HTTPS only)
- Private network blocking
- Dangerous pattern detection

### Request Safety
- Configurable timeouts (30s total, 10s connect)
- Proper User-Agent headers
- No arbitrary code execution
- Parameterized SQL queries

### Rate Limiting
- Per-endpoint rate limiting
- Configurable limits and time windows
- In-memory storage (Redis recommended for production)

## Best Practices for AI/LLM Integration

### Tool Selection Guidelines

1. **Start with NFL State**: Call `get_nfl_state` to determine current week/season
2. **Use Aggregators**: `get_fantasy_context` reduces API calls for common data
3. **Cache Awareness**: `fetch_teams` and `fetch_athletes` are expensive; call once
4. **Enrichment Trade-offs**: Advanced enrichment provides better insights but uses more resources
5. **Multi-week planning**: `get_bye_week_plan` for bye crunches, `get_ros_projections` / `find_trade_targets` for trades
6. **Waiver Intelligence**: `get_waiver_targets` for who to add; `get_waiver_log` for what already happened

### Performance Optimization

1. **Prefetch**: Enable prefetch for frequently accessed data
2. **Caching**: Leverage database caching for repeat queries
3. **Batch Operations**: Use aggregator tools to reduce API calls
4. **Limits**: Set appropriate limits to balance completeness and speed
5. **Timeouts**: Adjust timeouts based on network conditions

### Error Handling

1. **Check `success` field**: Always verify operation success
2. **Handle stale data**: Check `stale` flag for degraded responses
3. **Retry logic**: Server handles retries automatically
4. **Snapshot awareness**: Use snapshot data when fresh data unavailable
5. **Error messages**: Parse `error` field for actionable information

### Workflow Patterns

#### Start/Sit Decision Workflow
1. `get_weekly_briefing` → the recommended lineup, changes, injuries and IR moves in one call
2. `analyze_lineup` → grade what is currently set
3. `get_start_sit_recommendation` / `compare_players_for_slot` → the close calls, by name

#### Waiver Wire Research Workflow
1. `get_waiver_targets` → league-specific upgrades for your lineup
2. `recommend_faab_bid` → bid or claim timing
3. `get_waiver_log` → what the league already did (failed claims, re-entries)

#### Trade Evaluation Workflow
1. `find_trade_targets` → trades both lineups gain from (reads the trade deadline)
2. `analyze_trade` → evaluate the chosen proposal
3. `get_bye_week_plan` → check the bye weeks after the trade

#### Playoff Preparation Workflow
1. `get_playoff_odds` → where you stand
2. `get_strength_of_schedule(playoff_weeks=True, league_id=...)` → playoff-week matchups
3. `get_ros_projections` → playoff_points per player for stash/trade calls

## Response Format

All tools return consistent response structures:

### Successful Response
```json
{
  "success": true,
  "data": { ... },
  "count": 10,
  "timestamp": "2025-10-29T15:00:00Z"
}
```

### Error Response
```json
{
  "success": false,
  "error": "Descriptive error message",
  "error_type": "validation_error"
}
```

### Snapshot Response (Degraded)
```json
{
  "success": false,
  "stale": true,
  "retries_used": 3,
  "failure_reason": "timeout",
  "snapshot_fetched_at": "2025-10-29T14:45:00Z",
  "snapshot_age_seconds": 900,
  "data": { ... }
}
```

## API Endpoints

### MCP Endpoints
- **MCP Protocol**: `http://localhost:9000/mcp/`
- All tools accessible via MCP client

### REST Endpoints
- **Health Check**: `GET http://localhost:9000/health`
  - Returns: `{"status": "healthy", "service": "NFL MCP Server", "version": "0.5.8"}`

## Example Usage

### Python with FastMCP Client
```python
from fastmcp import Client

async with Client("http://localhost:9000/mcp/") as client:
    # Get current NFL state
    nfl_state = await client.call_tool("get_nfl_state", {})
    current_week = nfl_state.data["nfl_state"]["week"]
    
    # Get league matchups with enrichment
    matchups = await client.call_tool("get_matchups", {
        "league_id": "123456789",
        "week": current_week
    })
    
    # Analyze waiver activity (log, summary, re-entries)
    waivers = await client.call_tool("get_waiver_log", {
        "league_id": "123456789"
    })
    
    # Bye-week plan for the next six weeks
    plan = await client.call_tool("get_bye_week_plan", {
        "league_id": "123456789",
        "roster_id": 7,
        "weeks_ahead": 6
    })
```

## Version Information

- **Server Version**: 0.5.16
- **FastMCP Version**: 3.0+
- **Python Version**: 3.9+
- **Protocol**: Model Context Protocol (MCP)

## Additional Resources

- **Repository**: https://github.com/gtonic/nfl_mcp
- **FastMCP Documentation**: https://github.com/jlowin/fastmcp
- **Sleeper API**: https://docs.sleeper.com/
- **ESPN API**: Unofficial ESPN API integration

## Summary

The NFL MCP Server provides AI/LLM systems with comprehensive access to NFL and fantasy football data through a standardized MCP interface. With 30+ tools, advanced data enrichment, intelligent caching, and robust error handling, it enables sophisticated fantasy football analysis and decision-making workflows.

Key strengths:
- ✅ Comprehensive tool coverage (NFL data, fantasy leagues, player stats)
- ✅ Advanced enrichment (snap %, usage trends, practice status)
- ✅ Strategic planning (multi-week analysis, bye coordination, playoff prep)
- ✅ Robust design (retry logic, snapshot fallback, caching)
- ✅ Security-first (input validation, content sanitization, rate limiting)
- ✅ LLM-optimized (consistent responses, detailed metadata, error handling)

This server transforms raw NFL data into actionable fantasy football intelligence optimized for AI/LLM consumption.
