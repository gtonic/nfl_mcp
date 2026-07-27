# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Handcuff mapping** (`nfl_mcp/handcuff_tools.py` + new MCP tool
  `get_handcuff_map`) — for each RB on your Sleeper roster it reads the team
  depth chart, identifies the contingent-value backup, and flags whether that
  handcuff is a **free agent** (grab it), **yours** (secured) or an
  **opponent's**, listing securable free agents first. Turns the usual "secure
  your handcuffs" advice into an actionable list. Verified live (CMC → Jordan
  James, Saquon Barkley → Tank Bigsby); the depth-chart parsing was corrected to
  ESPN's real grid shape (each row is a starter + backups, injury tags stripped).
- **Win-probability lineup optimizer** (`nfl_mcp/win_probability.py` + new MCP
  tool `get_win_probability_lineup`) — optimizes **P(beating a specific
  opponent)** instead of E[points], the biggest strategic edge left in
  season-long fantasy. Each player is a `Normal(mean, sd)` (sd from the
  floor/ceiling band or position volatility); team totals combine so
  `P(win) = Φ(Δmean / √Σvar)` (exact). Lineup selection maximizes P(win) via
  local search over bench swaps, which **automatically recommends the ceiling
  lineup when you're the underdog and the floor lineup when you're favored**, and
  reports the win-probability gain over the points-optimal lineup. Composes with
  `project_players`. Credits **QB↔same-team pass-catcher (WR/TE) stacks** with a
  positive covariance (default ρ=0.35), so a stack's wider ceiling is valued when
  you're the underdog; the recommendation reports any stacks it used.
- **Opportunity-based projection baseline** (`nfl_mcp/opportunity.py` + new MCP
  tool `get_opportunity_projections`) — projects next-week PPR points from
  recency-weighted trailing **volume** (targets/carries; QB pass attempts) ×
  points-per-opportunity **shrunk toward a position prior**, instead of
  rank-bucket PPG. Volume is stickier than points, so it orders players better.
  **Backtested on real 2024 data (n=2505 player-weeks): MAE 5.78 → 5.70 (+1.4%),
  Spearman 0.470 → 0.494** — beating both the trailing-PPG baseline *and* the
  full matchup/usage multiplier stack, with the biggest gains at RB and TE. Wired
  into the backtest harness (`evals/backtest`) as an `opportunity` model so it
  stays measured. (Red-zone share isn't in the nflverse `player_stats` feed;
  making this the default `project_player` baseline is the justified next step.)
- **Weather / wind tool** (`nfl_mcp/weather_tools.py`) — new MCP tool
  `get_weather_forecast` reports per-game wind/precipitation/temperature from
  **Open-Meteo** (free, no API key) using static stadium coordinates + dome
  flags, and flags passing/kicking/running impact (wind ≥15 mph fades passing;
  kickers hit hardest; dome games neutral), worst-weather-first. Ships a
  reusable `weather_multiplier` heuristic. Live-verified against Open-Meteo.
- **Weather backtested and wired opt-in.** Added a `weather` model to
  `evals/backtest` (joins nflverse per-game recorded wind/roof to each
  player-week) to answer whether wind actually improves projections. Finding on
  2024: on the full slate the effect is within noise (MAE 5.783 → 5.782), but on
  the subset where it applies — passing (QB/WR/TE) in windy outdoor games,
  wind ≥ 15 mph, n=90 — it helps directionally (MAE 5.138 → 5.115, Spearman
  0.4938 → 0.4963). So the weather factor is **opt-in, not always-on**:
  `project_player`/`project_players` apply it only when the caller supplies
  `wind_mph`/`is_dome` (e.g. from `get_weather_forecast`), reported as
  `breakdown.weather_mult`. Small and rare, but real where it bites — and it
  never hurts.
- **Streaming planner** (`nfl_mcp/streaming_tools.py`) — new MCP tool
  `get_streaming_options` ranks weekly streaming options per position over the
  next 1-4 weeks: QB/RB/WR/TE by opponent defense-vs-position ease, **DST by
  opponent-offense weakness**, **K by own-offense strength**. Schedule-based and
  key-free. Adds `matchup_tools.fetch_offense_rankings` (nflverse points-scored
  per team, mirror of the defense rankings) to power the DST/K signals, with the
  same prior-season fallback (`offense_source_season` / `*_is_fallback`).
  Verified end-to-end against real 2025 data (Lions the #1 K stream, as
  expected). K improves further once the weather/wind factor lands. Optional
  `league_id` annotates each option with **free-agent availability** (clean for
  DST via the team-abbrev id; K/QB/TE/RB/WR list the team's players at that
  position), and `only_available=True` keeps just the streamers you can actually
  add.
- **Strength-of-schedule tools** (`nfl_mcp/sos_tools.py`) — new MCP tools
  `get_strength_of_schedule` (arbitrary week range) and `get_playoff_sos`
  (fantasy weeks 15-17) that rank NFL teams by schedule difficulty per position
  using the existing defense-vs-position rankings. Reports a 0-100 ease score
  (higher = softer schedule), ranks teams easiest-first, and — before a season
  has live data — transparently falls back to the prior season's defense
  rankings (`strength_source_season` / `strength_is_fallback`). Purely additive;
  does not touch the projection engine. Verified end-to-end against real 2025
  playoff-week schedules.
- **Pinned dependency lockfile for reproducible Docker builds** — added
  `requirements.lock` (228 fully-pinned transitive deps, generated via
  `uv pip compile requirements.txt --python-version 3.11`). The Dockerfile now
  installs from the lockfile instead of the floor-pinned `requirements.txt`, so
  image builds are deterministic. Regenerate with the command in the lockfile
  header when bumping `requirements.txt`.
- **Ruff linting with a CI gate** — added `[tool.ruff]` config (rule families
  `F`/`B`/`I`) and a `lint` job to CI that blocks the Docker build. This freezes
  the current quality: unused imports, undefined names, real bug patterns and
  import order now fail CI. The initial pass auto-removed ~150 unused imports and
  sorted imports across the codebase. Higher-volume stylistic debt (blind-except
  `BLE001`, `try/except/pass`, line length, whitespace, annotation style) is
  intentionally deferred to a follow-up cleanup so the gate lands green; a
  type-checker (mypy/pyright) is the planned next step.
- **`live` pytest marker** — tests that hit real external APIs (Sleeper/ESPN) are
  tagged `@pytest.mark.live` and skipped by default (opt in with `--run-live`,
  wired via `tests/conftest.py`). The unit suite now runs fully offline and
  deterministically; the same guarantees are covered by the data-source
  contracts watchdog under `evals/`.

### Changed
- **`project_player`/`project_players` default to the opportunity baseline** when
  `season` + `week` are supplied — the backtested opportunity projection
  (trailing nflverse volume × shrunk efficiency) replaces rank-bucket PPG as the
  base. The usage multiplier is skipped in that path (volume trend is already in
  the base) while matchup / environment / injury still apply. Players without
  enough nflverse history (rookies, preseason, K/DST) and calls that omit
  season/week transparently fall back to the rank-bucket baseline — fully
  backward compatible. The breakdown now reports `base_source`. This wires the
  measured EV win (PR #116) into the tools start/sit and lineups already use.
- **Docs split** — the README was slimmed from a ~58 KB monolith to a concise,
  feature-oriented overview (~140 lines) with a complete grouped catalog of all
  registered tools (including the new SOS / streaming / weather tools). Setup,
  configuration, architecture, data sources, the eval suite, CI/CD and security
  moved into a new **[docs/TECHNICAL.md](docs/TECHNICAL.md)**; the full per-tool
  reference stays in `AGENT.md`. Also corrected the stale "Python 3.9+"
  prerequisite (the package requires 3.11+) and added Open-Meteo to the
  documented data sources.

### Fixed
- **Package `authors` metadata** — replaced the `nfl@example.com` placeholder
  with the real maintainer (`gtonic <tom.geiger@alp54.com>`).
- **`get_draft_picks` silently returned un-enriched picks** — a duplicate,
  non-enriching definition later in `sleeper_tools.py` shadowed the intended
  enriched implementation (Python keeps the last definition), so callers never
  got the additive `player_enriched` field. Surfaced by the new lint gate
  (`F811`); removed the duplicate so enrichment is active again.
- **`requires-python` now correctly declares `>=3.11`** — the package imports
  `datetime.UTC` (Python 3.11+) across ~9 modules and `tomllib` in `health.py`,
  so it never actually ran on 3.10 despite `pyproject.toml` advertising `>=3.10`.
  CI only tests 3.11/3.12, so the mismatch went unnoticed. Dropped the stale
  `Programming Language :: Python :: 3.10` classifier; README badge, CI matrix
  and Dockerfile (`python:3.11-slim`) were already on 3.11.

### Security
- **SSRF hardening for `crawl_url`** — the only tool fetching arbitrary,
  caller-supplied URLs previously validated the scheme only. It now resolves
  the host and refuses any request to a loopback, private, link-local (incl.
  the `169.254.169.254` cloud-metadata endpoint), multicast, reserved or
  otherwise non-global address, normalizing decimal/octal/IPv6/IPv4-mapped IP
  literals. Redirects are followed manually (max 5 hops) with **every hop
  re-validated**, closing the redirect-into-private-network bypass. An
  intentional `NFL_MCP_ALLOW_PRIVATE_URLS` opt-in is available for trusted,
  isolated deployments. `validate_url_enhanced()` now uses the same
  `ipaddress`-based check for IP literals instead of brittle string prefixes.
  See [SECURITY.md](SECURITY.md) for the residual DNS-rebinding note and the
  network-exposure/authentication guidance.

## [0.6.0] - 2026-07-26

### Fixed
- **Draft starter requirements now count all Sleeper flex variants** — validating
  the live draft flow against a real 10-team league surfaced that
  `slots_rec_flex` (and `slots_wr_te`) were ignored, undercounting FLEX and
  skewing the roster-need weighting in `recommend_draft_pick`.

### Added
- **Draft-Day Playbook** (`docs/DRAFT_DAY.md`) + **live "war room" watcher**
  (`evals/live/draft_watch.py`): the playbook documents the full before/during
  draft workflow, how to read the recommendations, and where the tool leads
  (value rounds) vs where your judgment does (bench depth / handcuffs). The
  watcher polls a live Sleeper draft and recommends a pick each time you're on
  the clock, flipping to a bench-depth overlay once your starters are full.
  Distilled from a full live run against a real Sleeper draft.
- **Pre-draft flight check** (`evals/live/validate_draft.py`): runs the real
  Sleeper draft flow (`get_draft` → `get_draft_picks` → `recommend_draft_pick`)
  against your actual league/draft by username, league id or draft id — a
  green/red pre-flight before draft day. Verified end-to-end against a real
  completed Sleeper draft.
- **Evals — agent tool-routing** (`evals/agent/`, Eval Layer C): scenarios of
  realistic prompts → the tool(s) an assistant should call, with tool schemas
  derived from the live registry. A key-gated runner checks the model routes
  correctly (single-turn); offline guards (scenario/schema/registry validity,
  "every tool still has a description") run in normal CI so tool-description
  regressions are caught on every PR. On-demand `agent-evals.yml` workflow.
- **Evals — data-source contract checks** (`evals/contracts/`, Eval Layer B): a
  daily, non-blocking `contracts.yml` workflow that hits FantasyCalc / nflverse /
  Sleeper / ESPN and asserts the fields we depend on (`sleeperId`, `off_snp`,
  `opponent_team`, …) still exist — the early-warning system that would have
  caught the ESPN/FantasyPros defense-rankings breakage immediately. Critical
  failures fail the job; offline runner tests included.

### Changed
- **Matchup multiplier is now position-specific** (`matchup_multiplier()` in
  projections), tuned by the backtest: RB full weight, TE half, QB a quarter,
  WR off. The old flat ±10% over-adjusted and *hurt* QB/WR accuracy; the tuned
  version now improves projections on backtest instead of degrading them
  (measure → change → re-measure, see `evals/README.md`).

### Added
- **Evals — projection accuracy backtest** (`evals/backtest/`, Eval Layer A): a
  leak-free walk-forward backtest that measures whether the projection engine's
  multipliers beat a trailing-PPG baseline against real nflverse outcomes
  (MAE/RMSE/Spearman), and tunes the matchup strength. Imports the live constants
  so it evaluates production. Scheduled, non-blocking `evals.yml` workflow +
  `evals/README.md` documenting the 3-layer eval philosophy and findings.
  (Finding: the flat ±10% matchup multiplier over-adjusts and should be
  position-specific — helps RB/TE, hurts QB/WR.)
- **Playoff odds** (`playoff_tools.py`) — `get_playoff_odds` Monte-Carlos the rest
  of the regular season (each team scores ~ Normal around its points-per-game),
  ranks by record then points, and reports each team's playoff probability and
  average seed. Optional win/lose-this-week swing for your roster.

### Fixed
- **Defense-vs-position rankings now use real data** (nflverse weekly stats:
  fantasy points allowed per game, per defense, per position), replacing the
  broken ESPN/FantasyPros HTML paths that always fell back to alphabetical
  placeholders. This makes the matchup factor meaningful in-season for
  projections, start/sit and opponent analysis; in the preseason (no data yet)
  it honestly reports an `unknown` matchup instead of a fake rating.

### Added
- **Weekly projections** (`projections.py`) — transparent, no scraping/keys:
  `projected = base_ppg(position rank) × matchup × Vegas game environment × usage
  × injury`, with floor/ceiling, confidence and a full breakdown. Tools
  `project_player`, `project_players`. The lineup optimizer now **auto-fills
  projected points**, so start/sit works without manual point entry.
- **FAAB bid recommendations** (`faab_tools.py`) — `recommend_faab_bid` turns a
  waiver claim into a bid (% of budget + absolute) from real market value, the
  marginal upgrade for your roster, league demand (trending adds), and your
  remaining budget / weeks left, with a tier and transparent breakdown.

## [0.5.16] - 2026-07-19

### Added
- **Consensus player values** (`player_values.py`) backed by FantasyCalc (no API
  key), format-aware (PPR / superflex / league size / dynasty), cached in SQLite
  and memory. New tools: `get_player_values`, `get_player_value`.
- **Draft assistant** (`draft_tools.py`):
  - `get_draft_board` — tiered board ranked by Value-Based Drafting (VBD).
  - `recommend_draft_pick` — live Sleeper-draft recommendations with roster-need
    weighting, value-cliff and positional-run detection.
  - `simulate_draft` — offline snake-draft rehearsal (solo, repeatable) with
    realistic opponents, starting-lineup grading, and aggregate structure over
    many runs.
- **CI/CD pipeline** (`.github/workflows/ci.yml`): pytest on Python 3.11 & 3.12,
  then build and publish a Docker image to GHCR (`ghcr.io/gtonic/nfl_mcp`) on
  `main` and version tags; PRs build-only.
- `.dockerignore` (keeps local state out of the image), Dependabot config,
  and project docs (`CONTRIBUTING.md`, `SECURITY.md`, `CODEOWNERS`).

### Changed
- **Trade analyzer** now uses real market values instead of a flat 50-point
  heuristic; derives the league format from Sleeper settings and flags lopsided
  trades with value evidence.
- Matchup and Vegas tools surface fallback/placeholder data honestly (e.g. missing
  `ODDS_API_KEY`, no live defense data) instead of emitting confident-but-empty
  recommendations.

### Fixed
- Green test suite (previously 49 failing): response-schema drift, stale
  assertions, wrong patch targets, and two real bugs — a waiver `None`-comparison
  crash and a coaching role-classification substring mismatch.
- Aligned `requirements.txt` and `pyproject.toml` dependencies; documented
  `ODDS_API_KEY`; removed a stray dev script.

[Unreleased]: https://github.com/gtonic/nfl_mcp/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/gtonic/nfl_mcp/compare/v0.5.16...v0.6.0
[0.5.16]: https://github.com/gtonic/nfl_mcp/releases/tag/v0.5.16
