# 🏈 NFL MCP — Your AI Fantasy Football War Room

> **Win your draft. Dominate your season. With data, not gut feeling.**

[![CI](https://github.com/gtonic/nfl_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/gtonic/nfl_mcp/actions/workflows/ci.yml)
[![Data-source watchdog](https://github.com/gtonic/nfl_mcp/actions/workflows/contracts.yml/badge.svg)](https://github.com/gtonic/nfl_mcp/actions/workflows/contracts.yml)
[![Docker image](https://img.shields.io/badge/image-ghcr.io%2Fgtonic%2Fnfl__mcp-2496ED?logo=docker&logoColor=white)](https://github.com/gtonic/nfl_mcp/pkgs/container/nfl_mcp)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://github.com/gtonic/nfl_mcp)
[![70+ MCP tools](https://img.shields.io/badge/MCP%20tools-70%2B-8A2BE2)](#-whats-inside)
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
> **🏈 NFL MCP:** *Nacua — 18.7 projected (floor 11.6 / ceiling 25.8), ✅ high snap share, smash matchup. Start Nacua with confidence.*

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
- **Start/sit with automatic projections** — no manual point entry. `value × matchup × Vegas game-script × usage × injury`, with floor/ceiling and a transparent breakdown.
- **A real matchup edge** — which defense a player actually feasts on, from real weekly results (not a stale rankings page).
- **Streaming planner** — the best DST / K / QB / TE to stream over the next 1-3 weeks (soft defense, weak opposing offense, strong own offense).
- **Weather / wind** — fade passing and kickers in the ugly-weather games (wind ≥ 15 mph), dome games flagged neutral.

**🔄 Trades & waivers**
- **Trade analyzer on real market values** — knows your league's exact format and flags a lopsided deal *with evidence*.
- **FAAB bids** — exactly how much to spend on that waiver breakout (market value + league demand + your budget).

**🏆 Season strategy**
- **Monte-Carlo playoff odds** — *"72% to make it — 84% if you win this week."* Real probabilities, not vibes.
- **Strength of schedule** — rest-of-season and **playoff-week (15-17)** difficulty per position, for stash and trade-deadline calls.
- Bye-week coordination, trade-deadline timing, opponent-weakness scouting.

## ✅ Why you can trust it

- **Real data, zero gut-feeling heuristics** — market-consensus values ([FantasyCalc](https://fantasycalc.com)), real weekly stats ([nflverse](https://github.com/nflverse)), your live league ([Sleeper](https://sleeper.com)), news & injuries (ESPN), weather ([Open-Meteo](https://open-meteo.com)). **No paid API keys required to start.**
- **Honest about uncertainty** — when it lacks live data it *says so* (and falls back transparently) instead of faking a confident call.
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

## 🧰 What's inside

70+ MCP tools over HTTP, grouped by what they do. Every tool's full parameter reference
lives in **[AGENT.md](AGENT.md)**; below is the map.

**🎯 Draft & player values**
`get_draft_board` (VBD-tiered board) · `recommend_draft_pick` (best pick live) · `simulate_draft` (offline mock) · `get_player_values` / `get_player_value` (market consensus) · `analyze_trade` (fairness on real values) · `get_league_drafts` · `get_draft` · `get_draft_picks` · `get_draft_traded_picks`

**📊 Weekly lineup & projections**
`project_player` / `project_players` (transparent weekly points) · `get_start_sit_recommendation` · `get_roster_recommendations` · `compare_players_for_slot` · `analyze_full_lineup`

**🗓️ Matchup, schedule & environment**
`get_defense_rankings` · `get_matchup_difficulty` · `analyze_roster_matchups` · `get_strength_of_schedule` (ROS SOS) · `get_playoff_sos` (weeks 15-17) · `get_streaming_options` (DST/K/QB/TE streaming) · `get_weather_forecast` (wind/weather impact) · `get_strategic_matchup_preview` · `get_vegas_lines` · `get_game_environment` · `analyze_roster_vegas` · `get_stack_opportunities`

**🏆 Season strategy & opponents**
`get_playoff_odds` (Monte-Carlo) · `get_season_bye_week_coordination` · `get_trade_deadline_analysis` · `get_playoff_preparation_plan` · `analyze_opponent`

**💸 Waivers & FAAB**
`get_waiver_wire_dashboard` · `get_waiver_log` · `check_re_entry_status` · `recommend_faab_bid`

**🏈 Your Sleeper league**
`get_league` · `get_rosters` · `get_league_users` · `get_matchups` · `get_playoff_bracket` · `get_transactions` · `get_traded_picks` · `get_trending_players` · `get_fantasy_context` (aggregate) · `get_nfl_state` · `get_user` · `get_user_leagues` · `fetch_all_players`

**🩺 Injuries & availability**
`get_injury_report` · `get_high_confidence_injuries` (multi-source) · `get_gameday_inactives`

**📰 NFL data & news** (ESPN)
`get_nfl_news` · `get_teams` / `fetch_teams` · `get_depth_chart` · `get_team_injuries` · `get_team_player_stats` · `get_nfl_standings` · `get_team_schedule` · `get_league_leaders`

**🧠 Coaching intelligence**
`get_coaching_staff` · `get_all_coaching_staffs` · `get_coaching_tree` · `get_scheme_classification`

**📈 CBS Sports**
`get_cbs_player_news` · `get_cbs_projections` · `get_cbs_expert_picks`

**👥 Players / athletes**
`fetch_athletes` · `lookup_athlete` · `search_athletes` · `get_athletes_by_team`

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
