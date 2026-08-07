# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.5] - 2026-08-07

### Fixed
High-severity data-quality bugs found by the multi-agent tool audit (all
reproduced against live ESPN data):
- **`get_team_schedule` returned only the 3-game preseason slate.** The URL
  omitted `seasontype`, so ESPN defaulted to preseason before the season starts.
  Now requests `&seasontype=2` → the full 17-game regular season (and its bye gap),
  which also fixes the downstream bye/SoS/matchup previews.
- **`get_team_player_stats` always returned 0 players.** The URL carried a dead
  `/types/{season_type}/` segment (404 for every season, masked as
  `success=True, count=0`). Uses the season roster endpoint and dereferences the
  athlete `$ref`s → real players (id/name/position/jersey).
- **`get_nfl_standings` returned 4 placeholder rows with fabricated context.** The
  Core API endpoint only exposes standings-TYPE group refs, not teams. Switched to
  the site standings endpoint and parses `children[].standings.entries` → all 32
  teams with real records; preseason (0-0) no longer fabricates a motivation level.
- **`get_depth_chart` put player names in the `position` field.** ESPN renders
  each unit as a *pair* of tables (position labels + player grid); the parser now
  joins them, skips the `Starter` header, strips glued injury tags, and spaces the
  team name (`San Francisco 49ers`).
- **`get_high_confidence_injuries` was always empty.** Every ESPN injury got a
  flat confidence of 60, unreachable by the default `min_confidence=70`. Confidence
  is now graded by status certainty (Out/IR/Doubtful=90, Questionable=65, …), so
  the threshold surfaces the genuinely high-confidence injuries.
- **`get_league_leaders` was mis-wired.** The registry wrapper returned an
  undocumented shape, rejected the documented `passing`/`rushing` labels, and
  passed `limit` positionally into `season`. It now maps friendly labels to the
  short tokens, calls the underlying by keyword, applies `limit`, and returns the
  documented `{leaders, stat_type, count, season}` shape. (Note: ESPN's leaders
  endpoint itself yields no data in the preseason.)

Medium-severity fixes from the same audit:
- **`get_defense_rankings` (and the SoS/streaming/matchup tools it feeds) used a
  renamed nflverse asset URL** (`player_stats_*` → 404), so they collapsed to
  neutral placeholders even when real data existed. Updated to
  `stats_player/stats_player_week_{season}.csv` → real defense data again (e.g.
  2025 toughest QB defenses MIN/LAC/HOU).
- **`get_rosters` fabricated phantom "healthy" players** for Sleeper's empty-slot
  sentinel id `"0"` (9 per roster). The sentinel is now filtered before enrichment.
- **`project_player` over-credited confidence and hid the Vegas fallback.** It
  always passed an all-`None` usage dict (truthy → +15 confidence), reporting
  85/high vs `project_players`' 70/medium for identical input; confidence now
  requires a real usage signal. It also now surfaces `vegas_active` so a neutral
  Vegas placeholder isn't mistaken for a live implied total.
- **`get_playoff_preparation_plan` hard-coded a 4-week playoff** (`championship_week
  = start+3`); corrected to the standard 3-week span (`start+2`).
- **`get_injury_report` / `get_gameday_inactives` leaked the raw status enum**
  (`INJURY_STATUS_ACTIVE`) as `injury_type`; now uses the body part / readable
  description.
- **`get_stack_opportunities` gave no fallback disclaimer** when `ODDS_API_KEY`
  was unset (unlike `get_vegas_lines`); it now says so instead of reporting a bare
  "no high-total games".

The deeper medium-severity findings (logic derivation, not surgical fixes):
- **Bye weeks are now derived from the schedule gap.** `get_team_schedule` returns
  a `bye_week` (the one regular-season week 1-18 with no game — ESPN encodes a bye
  as a *missing* week, not a game row). `get_season_bye_week_coordination` and
  `get_strategic_matchup_preview` read it instead of matching a `"BYE WEEK"` string
  that was never emitted, so the bye calendar is populated again.
- **`get_playoff_odds` no longer invents odds with no schedule.** With
  `games_remaining == 0` (preseason / schedule unavailable) it returned a
  deterministic 100/0 split by roster id and a hard-coded `mean_ppg=100`; it now
  returns `computable: false` with an explanation.
- **`get_playoff_preparation_plan` stops fabricating a roster grade.** The
  readiness score reflects only preparation *timing*, so `roster_depth` /
  `schedule_strength` / `bye_week_planning` are now labelled "Not assessed" (with
  pointers to the tools that do assess them) instead of grading an empty pre-draft
  roster "Good".
- **`recommend_faab_bid` reframes non-FAAB leagues.** In a waiver-priority league
  the message now says to use a priority claim rather than "Bid ~75%".
- **`get_game_environment` returns its documented `game` field** (was missing →
  `KeyError` on `result['game']`).

Low-severity consistency/schema polish from the audit:
- **`get_teams` now returns team logos.** ESPN exposes images under `logos` (a
  list), not `logo`, so the field was empty for all 32 teams.
- **`get_draft_board` / `recommend_draft_pick` / `simulate_draft` echo the
  effective `ppr`.** The `format` block now includes the resolved `ppr` value, so
  an unrecognized `scoring` label (which maps to full PPR) is transparent.
- **`get_player_values` reports `updated_at` for fresh data** (fell back to the
  DB snapshot time, which is null on a fresh fetch → now uses `fetched_at`).
- **`analyze_roster_vegas` surfaces the Vegas fallback at the top level** (an
  `is_fallback` flag + a message note) instead of only per-entry.
- **`analyze_opponent` no longer rates an empty roster "100% vulnerable."** A
  pre-draft/empty opponent roster returns `no_data: true` (`vulnerability_score:
  null`) instead of fabricating "critical" weaknesses at every position.
- **`analyze_trade` resolves off-roster players' real identity.** A traded player
  not on the given roster is backfilled with the name/position from the value list
  (was `"Unknown (4034)"` despite a resolved value) and flagged in `warnings`.

## [0.7.2] - 2026-08-07

### Fixed
- **Scoring format is now normalized — `half_ppr` no longer silently means full
  PPR.** `scoring_to_ppr` only recognized `half-ppr` (hyphen); Sleeper's own
  `half_ppr` (underscore) and other spellings fell through to full PPR (1.0),
  so a half-PPR league silently got full-PPR values in `get_draft_board`,
  `get_player_value(s)`, `simulate_draft`, `recommend_draft_pick`, etc. It now
  normalizes separators/case and accepts `half_ppr`/`halfppr`/`HALF PPR`/`std`/
  `none`/raw numbers.
- **`get_defense_rankings` no longer emits a fake ranking in the preseason.** The
  neutral fallback (used when nflverse data isn't published yet) ranked teams
  `1..32` in **alphabetical** order and the tool returned it with no fallback
  flag — so matchup grades looked real but were alphabetical. The fallback is now
  genuinely neutral (all teams rank 16) and the response carries `is_fallback`
  plus a ⚠️ low-confidence message, mirroring `get_strength_of_schedule`.

### Added
- **`get_weather_forecast` now reports `forecast_unavailable`.** Games beyond
  Open-Meteo's ~16-day horizon were already flagged per-game as
  `impact.severity="unknown"`; the response now also carries a top-level count
  (and a message note) so callers planning ahead aren't misled by empty weather
  fields reading as "calm".

## [0.7.1] - 2026-08-07

### Fixed
- **`get_team_injuries` now returns real injury data (names, status, body part,
  return date).** ESPN's Core API returns the team injury list as bare
  `{"$ref": …}` links; the tool treated each item as a complete object, so every
  field came back empty (`player_name: null`, `status: "Unknown"`). It now
  follows both hops — the injury ref and the nested athlete ref — concurrently
  (bounded fan-out), and surfaces `player_name`, `position`, `status`, `type`
  (body part), `description`, `return_date` and a fantasy `severity`. Inline
  responses are still handled (back-compat), and "Injured Reserve" now maps to
  `High` severity.
- **`get_coaching_staff` now resolves the head coach and fills in coordinators.**
  The ESPN coach object has no `displayName`/`position`, so the coach name came
  back empty and no head coach was identified; the name is now built from
  `firstName`/`lastName` and the coach returned by ESPN's team `/coaches`
  endpoint (which exposes only the head coach) is promoted to `head_coach`.
  Because ESPN never exposes coordinators, `offensive_coordinator` /
  `defensive_coordinator` are now enriched **best-effort from the Wikipedia
  season-page infobox** (`off_coach`/`def_coach`) — with a `coordinator_source`
  field and an honest `note` about the partial coverage. Adds an optional
  `season` argument (defaults to the current NFL season).
- **`get_all_coaching_staffs` now returns head coaches (was 0/32).** The coaches
  URL was built as `f"{team_url}/coaches"` where `team_url` already carried a
  `?lang=…` query string, producing a broken URL (`…?lang=…/coaches`), so every
  team came back `head_coach: null, coach_count: 0`. The URL is now built from
  the numeric team id and the head-coach name is resolved from `firstName`/
  `lastName`.

## [0.7.0] - 2026-08-05

### Changed
- **Upgraded to FastMCP 4 (`>=4.0.0b1`) and moved the HTTP transport to stateless
  MCP (the sessionless `2026-07-28` protocol).** FastMCP 4 rebuilds on MCP Python
  SDK v2 and serves the sessionless `2026-07-28` protocol via mode negotiation.
  `main()` now enables `stateless_http` on `app.http_app(path="/mcp", …)`, so the
  Streamable HTTP transport keeps **no** server-side session state and issues no
  `Mcp-Session-Id` — the server can scale horizontally behind a plain round-robin
  load balancer with no sticky sessions or shared session store. Set
  `NFL_MCP_STATELESS_HTTP=0` to fall back to the session-based transport for
  older, handshake-era clients. The background-prefetch lifespan is now registered
  through FastMCP's public `lifespan=` constructor argument instead of
  monkey-patching the ASGI app's internal `router.lifespan_context`. No tool
  implementations changed; the codebase used none of the removed v4 APIs
  (`ctx.sample`/`list_roots`/`elicit`, constructor transport kwargs, etc.). Full
  suite green (906 passed, 2 skipped). See `FASTMCP_4_UPGRADE.md`.
- **Pinned `httpx<1` and `pydantic<2.14`.** Defensive upper bounds so a
  `uv`/`pip` resolution during the FastMCP 4 beta can't pull `httpx==1.0.dev*`
  (breaking-major dev release) or `pydantic==2.14.0a*` (alpha). `requirements.lock`
  was regenerated with `--prerelease=allow` accordingly.
- **`get_trending_players` now surfaces `full_name`/`position`/`team` at the top
  level of each entry, with a team fallback.** Previously the identity fields were
  only reachable under the nested `enriched` object, so naive consumers saw bare
  Sleeper player IDs. The three key fields are now mirrored to the top level
  (additive — `enriched` is unchanged), and `team` falls back to the raw Sleeper
  `team` field when the athlete cache's `team_id` column is blank (genuine free
  agents stay `null`).

### Fixed
- **`get_nfl_news` HTTP 403 from ESPN — branded User-Agent now identifies via a
  project URL.** ESPN's `site.api.espn.com` WAF started rejecting the plain
  `NFL-MCP-Server/<version> (…)` User-Agent with `403 Forbidden`, which broke
  `get_nfl_news` (and would also affect the `teams`/`scoreboard` `site.api`
  endpoints). The base User-Agent now embeds the standard bot-identification
  comment `(+https://github.com/gtonic/nfl_mcp)`, which ESPN accepts. As a
  belt-and-suspenders fallback, `get_nfl_news` retries once with httpx's default
  User-Agent if a `403` still comes back.

### Added
- **Periodic athletes-cache refresh in the background prefetch.** Player→team
  assignments change over the offseason and season (signings, trades, releases),
  which left the Sleeper athletes cache stale — e.g. trending players showing an
  outdated team. When prefetch runs (`NFL_MCP_PREFETCH=1` + `NFL_MCP_ADVANCED_ENRICH=1`)
  the athletes cache is now refreshed once at startup and then on a cadence
  (default **daily**). Controlled by `NFL_MCP_PREFETCH_ATHLETES` (default on) and
  `NFL_MCP_PREFETCH_ATHLETES_INTERVAL` (seconds, default `86400`); the refresh is
  best-effort (failures are logged, never fatal) and its config is surfaced in the
  `/health` `prefetch` block.
- **Configurable database path via `NFL_MCP_DB_PATH`.** The SQLite cache path now
  falls back to the `NFL_MCP_DB_PATH` environment variable (default
  `nfl_data.db`). The default is resolved inside `NFLDatabase.__init__`, so
  *every* `NFLDatabase()` call in the codebase honors it — point it at a mounted
  volume (e.g. `NFL_MCP_DB_PATH=/data/nfl_data.db` + `-v nfl-mcp-data:/data`) to
  persist the warmed cache across container restarts instead of re-initializing
  each time. An explicit `db_path` argument (e.g. a test's temp file) still wins.

## [0.6.7] - 2026-07-27

### Security
- **Multi-stage Docker build — drops `gcc`/`binutils` from the runtime image.**
  The build stage keeps the C toolchain (to compile any sdist-only dependency)
  and installs everything into an isolated virtualenv; the runtime stage copies
  only that venv, so the shipped image carries no `gcc`/`binutils`. `binutils`
  was the source of the large majority of the image scanner's findings (broad
  CVE surface, reported across ~8 sub-packages), so this removes that entire
  class of CVEs — and shrinks the image. The remaining findings are Debian
  base-OS packages Debian itself rates *negligible* with no fix available
  (`under investigation`), inherent to any Debian-based image.

## [0.6.6] - 2026-07-27

### Changed
- **Started splitting the `sleeper_tools.py` monolith — enrichment layer
  extracted.** Moved the ~1,000-line enrichment/data-fetch layer (schedule,
  snaps, injuries, practice reports and weekly-usage fetchers + the
  usage/opponent enrichment helpers, plus `ADVANCED_ENRICH_ENABLED`) into a new
  `nfl_mcp/sleeper_enrichment.py`. `sleeper_tools.py` re-exports it, so every
  `sleeper_tools.<name>` reference and import is unchanged — a behavior-preserving
  refactor. Full suite green; a few white-box tests were repointed to patch the
  enrichment module directly.
- **Split the `sleeper_tools.py` monolith — strategic-planning cluster
  extracted.** Moved the ~670-line strategic-planning tools
  (`get_strategic_matchup_preview`, `get_season_bye_week_coordination`,
  `get_trade_deadline_analysis`, `get_playoff_preparation_plan`) into a new
  `nfl_mcp/sleeper_strategy.py`. These are top-level *consumers*, so
  `sleeper_tools.py` re-exports them at the end of the module (after the core
  tools exist) to keep the imports acyclic. With enrichment + strategy out,
  `sleeper_tools.py` is now ~1,370 lines (from 3,022). Behavior-preserving; a few
  white-box tests were repointed to patch the strategy module.
- **Split the `sleeper_tools.py` monolith — transactions extracted.** Moved
  `get_transactions` (week-inferring, robust) and `get_traded_picks` into a new
  `nfl_mcp/sleeper_transactions.py`, importing the primitives they need
  (`get_nfl_state`, `_init_db`, `_enrich_single`, `_enrich_usage_and_opponent`)
  and re-exported from `sleeper_tools`. With enrichment + strategy + transactions
  out, **`sleeper_tools.py` is now ~1,110 lines (from 3,022 — a 63% reduction)**;
  league/roster is the remaining core. Behavior-preserving; the transactions
  white-box tests were repointed to the new module.

### Fixed
- **Strategic-planning error paths crashed on an upstream failure.** All four
  strategic tools passed their fallback `data` dict positionally to
  `create_error_response()`, which landed in the `error_type` parameter and
  collided with the `error_type=` keyword (`TypeError: got multiple values`).
  The branch was never exercised until the split's tests hit it. Fixed (pass
  `data=`) and added a regression test.

### Security
- **Container-image hardening (CVE reduction).** The Docker build now runs
  `apt-get upgrade` to pull Debian security patches (glibc/ncurses/sqlite/zlib/…),
  upgrades the bundled pip build tooling (`wheel`/`setuptools` — clears the
  `wheel` advisory), and **drops `curl` from the image** (the health check now
  uses Python's stdlib), removing the curl/libcurl findings. `requirements.lock`
  already pins a fixed `jaraco-context` (6.1.2).
- **Docker base bumped to `python:3.13-slim`** (from 3.11-slim) to clear the
  CPython stdlib CVEs the scanner flagged (fixed only in 3.13+). `requirements.lock`
  was regenerated for 3.13 (drops the 3.11-only backports), and the CI test matrix
  now includes **3.13** so the container's runtime is actually tested. The app
  still supports 3.11+ (`requires-python >=3.11`). Remaining image findings are
  Debian packages still "under upstream investigation" — mitigated by rebuilding
  regularly.

## [0.6.5] - 2026-07-27

### Added
- **Type checking (mypy) as a report-only CI job** — added a `[tool.mypy]`
  config (lenient, `check_untyped_defs`) and a **non-blocking** `typecheck` CI
  job. It's a signal while the code is progressively typed, not a merge gate.
  The first pass already caught real bugs (see Fixed).
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
- **Lint-debt cleanup + expanded ruff gate** — grew the ruff rule set from
  `F`/`B`/`I` to also enforce whitespace (`E`/`W`), pyupgrade modernization
  (`UP`), simplifications (`SIM`), comprehensions (`C4`), `PIE`, Ruff-native
  checks (`RUF`) and timezone-aware datetimes (`DTZ`), and auto-fixed ~4,800
  findings in one sweep: trailing/blank-line whitespace, Python-3.11 native type
  annotations (`List[x]`→`list[x]`, `Optional[x]`→`x | None`), f-string
  conversions, comprehension and redundancy cleanups. Also fixed the deprecated
  `datetime.utcnow()` → `datetime.now(UTC)`. Deliberately **not** enforced
  (documented in `pyproject.toml`): blind-except `BLE001` (an intentional
  best-effort pattern here), line length `E501`, ambiguous-unicode (emoji/text),
  and a short list of manual-only style nits. No behavior change; full suite
  green. A type checker (mypy/pyright) is the planned next step.
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
- **Missing `ErrorType.API_ERROR` / `ErrorType.NOT_FOUND`** — several error paths
  in `sleeper_tools.py`/`nfl_tools.py` referenced these enum members, which
  didn't exist, so hitting those paths raised `AttributeError`. Added the
  members. Surfaced by the new mypy pass.
- **Missing `database.get_nfl_database()`** — `nfl_tools` imported this factory
  (advanced-enrichment cache paths) but it was never defined, raising
  `ImportError` at runtime. Added it. Surfaced by the new mypy pass.
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
