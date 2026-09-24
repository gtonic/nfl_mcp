# 🏈 NFL MCP — Your AI Fantasy Football War Room

> **Win your draft. Dominate your season. With data, not gut feeling.**

[![CI](https://github.com/gtonic/nfl_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/gtonic/nfl_mcp/actions/workflows/ci.yml)
[![Data-source watchdog](https://github.com/gtonic/nfl_mcp/actions/workflows/contracts.yml/badge.svg)](https://github.com/gtonic/nfl_mcp/actions/workflows/contracts.yml)
[![Docker image](https://img.shields.io/badge/image-ghcr.io%2Fgtonic%2Fnfl__mcp-2496ED?logo=docker&logoColor=white)](https://github.com/gtonic/nfl_mcp/pkgs/container/nfl_mcp)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://github.com/gtonic/nfl_mcp)
[![55 in-season MCP tools](https://img.shields.io/badge/MCP%20tools-55%20in--season%20%7C%2072%20total-8A2BE2)](#-whats-inside)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

NFL MCP turns real NFL & fantasy data into a decisive edge — a suite of tools that plug
straight into your AI assistant (Claude Desktop, Claude Code, Cursor, …). Ask a plain
question, get a data-backed answer: *who to draft, who to start, whether that trade is a
fleece, and what your playoff odds really are* — for **your** league, **your** roster,
**your** pick, in real time.

This isn't another rankings site you tab away from mid-draft. It lives **inside your
assistant** and answers the question you actually asked.

## 💬 See it in action

> **You:** *"I'm on the clock at 1.09 in my 12-team half-PPR draft — who do I take?"*
>
> **🏈 NFL MCP:** *Jahmyr Gibbs (RB) — top value here (VBD 8787), and there's a **value cliff at RB after him (−1995)**. Elite WRs are deeper, so lock the RB now and grab WR at the turn.*

> **You:** *"Start Puka Nacua or DeVonta Smith this week?"*
>
> **🏈 NFL MCP:** *Nacua — 18.7 projected vs Smith's 11.2, a 7.5-point edge (floor 4.9 / ceiling 32.5). Clear choice.*

> **You:** *"Is trading my Bijan for their CeeDee + a WR2 fair?"*
>
> **🏈 NFL MCP:** *Slightly favors you (fairness 82/100) — two startable pieces beat one stud for your thin WR room. ⚠️ You'd drop to 3 RB, so mind the depth.*

<sub>Real tool outputs — value cliffs, projections with floor/ceiling, market-value trade fairness — rendered by your assistant.</sub>

## 🔥 Why you'll win

**🎯 Draft day**
- **VBD draft board** ranked by *value over replacement* — the ordering that wins drafts, not raw ADP.
- **Live "war room"** — during your real Sleeper draft it reads the board live and calls the best pick *for your roster*, with **value-cliff** warnings and **positional-run** alerts.
- **Rehearse first** — run 100 mock drafts from your slot before you're on the clock.

**📊 Every week**
- **Start/sit with automatic projections** — no manual point entry. **Sleeper-first:** `0.25 × our model + 0.75 × Sleeper's projection`, where our model is `opportunity (regressed early-season) × matchup × Vegas game-script × usage × injury & practice` and falls back alone (labelled `model_only`) when Sleeper has no number; both priced in **your league's own Sleeper scoring settings** (not just PPR / half-PPR presets), with floor/ceiling and a transparent breakdown. **Decided on projected points**, with a verdict scaled to the model's own error, so a half-point difference is reported as a coin flip rather than an edge.
- **A real matchup edge** — which defense a player actually feasts on, from real weekly results (not a stale rankings page).
- **Streaming planner** — the best DST / K / QB / TE to stream over the next 1-3 weeks (soft defense, weak opposing offense, strong own offense).
- **Weather / wind** — fade passing and kickers in the ugly-weather games (wind ≥ 15 mph), dome games flagged neutral.
- **Game-day aware** — players whose game has kicked off are locked, byes are caught from the schedule, practice reports (DNP / LP / FP) and official inactives feed straight into the call.

**🔄 Trades & waivers**
- **Trade analyzer on real market values** — knows your league's exact format and flags a lopsided deal *with evidence*.
- **FAAB bids or waiver priority** — exactly how much to spend on that waiver breakout (market value + league demand + your budget); in non-FAAB leagues, whether the add is worth burning your waiver priority.
- **Rest-of-season aware** — waiver and trade gains are measured over the rest of the season and your fantasy-playoff weeks, not just this week.

**🏆 Season strategy**
- **Monte-Carlo playoff odds** — *"72% to make it — 84% if you win this week."* Real probabilities, not vibes (median-game leagues included).
- **Strength of schedule** — rest-of-season and **fantasy-playoff-week** difficulty per position, for stash and trade-deadline calls.
- **Bye-week plan** — which upcoming week leaves a slot empty, what to add and who is free; opponent-weakness scouting.

## ✅ Why you can trust it

- **Real data, zero gut-feeling heuristics** — market-consensus values ([FantasyCalc](https://fantasycalc.com)), real weekly stats ([nflverse](https://github.com/nflverse)), your live league ([Sleeper](https://sleeper.com)), news & injuries (ESPN), practice reports (NFL.com), weather ([Open-Meteo](https://open-meteo.com)), Vegas lines ([The Odds API](https://the-odds-api.com), optional `ODDS_API_KEY`). **No paid API keys required to start.**
- **Honest about uncertainty** — when it lacks live data it *says so* (and falls back transparently) instead of faking a confident call. If Sleeper is down and only an old roster snapshot is left, availability questions (waivers, FAAB, free-agent handcuffs) are refused rather than answered on stale rosters.
- **It grades its own accuracy.** A built-in backtest measures whether its projections actually beat a baseline on real past seasons, and a daily watchdog alerts if a data source changes. *Most fantasy tools never check whether they're right. This one does.*

## ⚡ 60-second start

```bash
docker run --rm -p 9000:9000 ghcr.io/gtonic/nfl_mcp:latest
```

Connect it to your assistant, then just ask:

> *"My Sleeper username is `gary` — find my league, build my draft board, and simulate a draft from my slot."*

**Claude Code (CLI):**
```bash
claude mcp add --transport http nfl-mcp http://localhost:9000/mcp/
```

**Claude Desktop / Cursor** — bridge via [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):
```json
{
  "mcpServers": {
    "nfl-mcp": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:9000/mcp/"] }
  }
}
```

Full setup, configuration and deployment → **[docs/TECHNICAL.md](docs/TECHNICAL.md)**.
Draft-day walkthrough → **[docs/DRAFT_DAY.md](docs/DRAFT_DAY.md)**.

Co-managers work too: pass your own Sleeper `user_id` and the roster is found via `co_owners`.

## 🧰 What's inside

55 MCP tools in the default in-season profile (72 in total), grouped by what they do.
Every tool ships its own parameter schema over MCP, so your assistant can introspect
them directly; **[AGENT.md](AGENT.md)** documents all of them plus integration guidance.

**Tool profiles** — fewer, non-overlapping tools route better. Set `NFL_MCP_TOOL_PROFILE`:

| Profile | Tools | Hides |
|---|---|---|
| `season` (default) | 55 | draft (8), coaching (4), cache-refresh admin (3), `get_league_leaders`, `get_cbs_expert_picks` |
| `offseason` | 43 | in-season-only tools (lineups, waivers, Vegas, weather, byes, playoff odds, …), admin, `get_cbs_expert_picks` |
| `full` | 72 | nothing |

The active profile and its tool count are logged at startup and reported by `GET /health` (`tools`).

**📊 Weekly lineup & projections**
`get_weekly_briefing` (**start here** — roster, opponent, weather, usage and the lineup changes worth making, in one call; mid-week it scores played games for real and only optimizes what you can still change) · `get_league_changes` (**daily check** — what moved for your roster since you last looked) · `analyze_lineup` (grade the lineup you have set, optimal vs current, swaps, locked players) · `get_start_sit_recommendation` (one player or a list; team, position, opponent, snaps, injury and practice looked up for you) · `compare_players_for_slot` · `get_win_probability_lineup` (lineup that maximises P(beating *this* opponent)) · `project_players` (this week's points, one or many, with Sleeper's projection as a labelled second opinion) · `get_ros_projections` (rest-of-season + fantasy-playoff points in your scoring) · `get_opportunity_projections` · `get_usage_trends` · `get_weekly_retro` (after the games: actual vs projection, points left on the bench)

**🗓️ Matchup, schedule & environment**
`get_bye_week_plan` (which upcoming weeks byes leave your lineup short, what to add, free agents who fill it) · `get_defense_rankings` (all defenses, or one via `opponent_team`) · `analyze_roster_matchups` (your roster's matchups this week) · `get_strength_of_schedule` (any week range, or `playoff_weeks=True` from your league's playoff window) · `get_streaming_options` · `get_weather_forecast` · `get_vegas_lines` (games, per-team environment via `teams`, or your whole roster via `league_id`+`roster_id`) · `get_stack_opportunities`

**🔄 Trades, waivers & FAAB**
`get_waiver_targets` (**who to pick up** in your league) · `recommend_faab_bid` · `get_waiver_log` (processed/failed claims, summary, re-entries; `sections`, `player` filter) · `audit_ir_slots` · `get_handcuff_map` · `find_trade_targets` (trades both lineups gain from; reads the trade deadline) · `analyze_trade` · `get_player_values` (market consensus; one or many players)

**🏆 Season strategy & opponents**
`get_playoff_odds` (Monte-Carlo) · `analyze_opponent`

**🏈 Your Sleeper league**
`get_league` · `get_rosters` · `get_league_users` · `get_matchups` · `get_playoff_bracket` · `get_transactions` · `get_trending_players` · `get_fantasy_context` (aggregate) · `get_nfl_state` · `get_user` · `get_user_leagues`

**🩺 Injuries & availability**
`get_injury_report` (who is hurt: teams / players, `min_confidence`, `severity`, `since`, practice reports; healthy rows only with `include_healthy`; ESPN source) · `get_injury_trends` (**what changed** since you last looked) · `get_gameday_inactives`

**📰 NFL data & news**
`get_nfl_news` · `get_teams` · `get_depth_chart` · `get_team_player_stats` · `get_nfl_standings` · `get_team_schedule` · `get_cbs_player_news` · `get_cbs_projections` (CBS **season-long** totals, not weekly)

**👥 Players / athletes**
`lookup_athlete` · `search_athletes` · `get_athletes_by_team`

**🎯 Draft** (`offseason` / `full`)
`get_draft_board` (VBD-tiered board) · `recommend_draft_pick` (best pick live) · `simulate_draft` (offline mock) · `get_league_drafts` · `get_draft` · `get_draft_picks` · `get_draft_traded_picks` · `get_traded_picks`

**🧠 Coaching intelligence** (`offseason` / `full`)
`get_coaching_staff` · `get_all_coaching_staffs` · `get_coaching_tree` · `get_scheme_classification`

**🛠️ Admin & niche** (`full`; `get_league_leaders` also `offseason`)
`fetch_athletes` · `fetch_all_players` · `fetch_teams` (cache refreshes the prefetch loop already runs) · `get_league_leaders` · `get_cbs_expert_picks` (CBS experts' ATS betting picks)

**🌐 Web & health**
`crawl_url` (SSRF-guarded text extraction) · `GET /health` (REST)

## 📚 More

- **[docs/TECHNICAL.md](docs/TECHNICAL.md)** — setup, configuration, architecture, data sources, eval suite, CI/CD, security.
- **[AGENT.md](AGENT.md)** — full per-tool reference for AI agents.
- **[docs/DRAFT_DAY.md](docs/DRAFT_DAY.md)** — before/during draft-day playbook.
- **[SECURITY.md](SECURITY.md)** — security policy, SSRF protections, reporting.
- **[CHANGELOG.md](CHANGELOG.md)** — release history.

## License

MIT — see [LICENSE](LICENSE).
