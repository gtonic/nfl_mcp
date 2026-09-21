# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **`get_transactions` now says that pending waiver claims are invisible to it.**
  Sleeper exposes a claim only once it has been *processed*; a claim sitting in a
  manager's queue appears in no API response. The docstring did not mention it,
  and the natural reading of an empty list — "nobody has claims in" — produced a
  confident "you have no claims pending" while two were waiting in the league
  app. There is no public endpoint for pending claims, so this is documentation
  rather than a code fix, with a test asserting the warning stays put.

### Fixed
- **`InjuryAggregator` used outside `async with` failed silently.** Without a
  context manager `self._http_client` is None, so every team fetch raised
  `'NoneType' object has no attribute 'get'` — which the per-team handler logged
  at *debug* level as "ESPN page 1 failed for BUF". That reads like a broken
  upstream payload, and it cost a real debugging session against an ESPN feed
  that turned out to be perfectly intact. All 32 teams then returned nothing and
  the caller got an empty, successful-looking result that was indistinguishable
  from "no injuries in the league".

  Both public entry points now check first and raise a message naming the two
  ways to fix it, plus why the old behaviour was dangerous. Passing
  `http_client=` explicitly remains supported.

### Added
- **`get_waiver_targets` reports the league's waiver configuration** as
  `waiver_rules`: priority vs FAAB, daily vs weekly processing, the waiver
  weekday, clear days, and what the configuration does *not* settle.

  It deliberately does **not** return an "instant add vs claim" verdict. That
  was inferred twice in live use and was wrong both times — first by reasoning
  "never dropped, therefore not on waivers, therefore instant" (false under
  weekly waivers, where the whole free-agent pool is locked during the game
  week), then by reading `daily_waivers=1` as instant (also false; the league's
  own app showed the claim processing on the waiver day anyway). Which days
  daily waivers run is encoded in `daily_waivers_days` as a bitmask, and the
  observed processing time matches none of the exposed fields.

  So the output names the bitmask it cannot decode and points at the app, which
  shows the real answer per player. `how_to_confirm` also states that pending
  claims are not exposed by the API at all — their absence from a transaction
  list does not mean none exist, which is the inference that produced a
  confident "you have no claims pending" while two were.

### Added
- **Every roster tool now reports how old its data is.** `get_weekly_briefing`,
  `get_waiver_targets` and `find_trade_targets` return `data_freshness` (age in
  hours per feed: injuries, athletes, practice status) plus
  `stale_data_warnings` in plain language.

  This is the one place the codebase was not honest about a guess. Vegas lines
  carry `is_fallback`, defense rankings carry `stale`, the scheme table carries
  `as_of` — but a start/sit recommendation built on a day-old injury report
  looked exactly like one built on a fresh one. Found the hard way: gameday
  advice was given against a **32-hour-old** injury feed, noticed only by
  querying `MAX(updated_at)` by hand.

  The thresholds are deliberately tight for injuries (6h) and loose for the
  athlete cache (36h), because designations flip in the last hours before
  kickoff while roster membership does not. A feed with no rows reports
  `age_hours: None` rather than 0 — "never fetched" and "just fetched" must not
  look alike.

### Fixed
- **The weekly briefing read only one of the two injury feeds.**
  `_injury_status` took Sleeper's player list; the multi-source ESPN/CBS reports
  sitting in `player_injuries` — with severity and confidence — were never
  consulted. Around kickoff Sleeper is routinely the slower of the two, and
  taking the milder reading is what puts a hurt player in a lineup: on
  2026-09-20 ESPN had Brock Bowers at **doubtful** while Sleeper still said
  **questionable**, so the briefing applied a 0.9 multiplier and recommended
  starting him over a healthy tight end. He did not play.

  Both feeds are now combined and the **more severe** designation wins — erring
  toward the bench is the recoverable direction. The two id spaces are
  unrelated (`player_injuries` carries ESPN athlete ids, `athletes` carries
  Sleeper ids; 12 of 2638 rows collide by accident), so the join goes through
  the normalized name, reusing the same `norm_name` the nflverse lookup uses.

  The response gains `injury_source_conflicts`, naming every player where the
  feeds disagreed and which reading was used — the lineup call made on the
  milder one is exactly the thing worth seeing. `worst_status` treats an
  unrecognised designation as MODERATE rather than best-casing it, so a feed
  change cannot quietly downgrade the whole roster.

## [0.8.2] - 2026-09-19

A release about numbers that were confidently wrong. Nothing here crashed or
errored — every entry under *Fixed* produced a plausible-looking figure that
happened not to mean what it said: points on the wrong scale, an uncertainty
band half as wide as reality, a variance nobody had ever measured, a curated
table presented with the confidence of a live fetch.

Three of them are the same mistake at different levels. A hand-picked constant
carrying a number the user reads — the per-reception value, the floor/ceiling
volatility, the weekly scoring spread — was replaced by a measurement, and in
each case the measurement disagreed with the constant. The projection band
covered 36% where it claimed 68%; the win probability called matchups at 96%
that were won 79% of the time.

*Added* closes the two missing thirds of the weekly cycle. `get_weekly_briefing`
already answered "how do I line up"; `get_waiver_targets` and
`find_trade_targets` answer "who do I pick up" and "who do I trade with",
both from the same measurement of which players actually win a starting slot.

### Added

- **`find_trade_targets` — finds the deal instead of grading one.**
  `analyze_trade` evaluates a trade you already have in mind; the harder half of
  the question came first and had no tool. Answering it by hand means reading
  eleven rosters and guessing who is thin where.

  Every one-for-one swap against every other roster is scored by recomputing
  **both** teams' best legal starting lineup before and after it, with the same
  optimizer the weekly briefing uses — so FLEX and SUPERFLEX are filled the way
  the league fills them, and a surplus player who never cracks the lineup
  correctly costs nothing to trade. A proposal survives only if both totals go
  up: a trade the other manager loses is a wish, not a deal. One proposal per
  partner, so the output is a set of conversations to have rather than twenty
  variations on one.

  Deliberately one-for-one, and deliberately weekly: the gains are this week's
  lineup points, not rest-of-season value, which the response says in
  `caveats` and which `analyze_trade` is there to check.

  The roster maths behind it (`slot_counts`, `replacement_levels`,
  `lineup_total`, `surplus_players`) moved into `nfl_mcp/roster_needs.py`, since
  the waiver question and the trade question reduce to the same measurement;
  `get_waiver_targets` now uses that one implementation.

- **`get_waiver_targets` — the waiver question finally has a tool.** The weekly
  cycle is lineup / waivers / trades: `get_weekly_briefing` answered the first
  and `analyze_trade` the third, while the middle one had only adjacent tools.
  `get_trending_players` is league-agnostic and includes players already
  rostered in your league, `recommend_faab_bid` needs you to know the name
  already, and `get_waiver_wire_dashboard` reads the transaction log rather than
  the pool.

  This starts from the pool: every athlete nobody in the league rosters,
  projected for the coming week in the league's own scoring, ranked by the only
  thing that makes a claim worth making — points above the weakest player who
  currently starts for you at that position. Flex seats are spread across the
  positions that can fill them, so the replacement level reflects how deep you
  actually start. Returns drop candidates and thin positions alongside.

  It only considers positions the league *starts*: a league with no kicker slot
  was otherwise told to claim kickers, which scored as a large upgrade precisely
  because there was no kicker to compare against. Without live Vegas lines,
  defenses and kickers are priced off a constant, so they are reported as
  `no_signal` instead of being dressed up as a ranking. When nothing beats your
  starters it says so, rather than ranking players who would all make the lineup
  worse.

- **The weekly briefing scores a matchup in progress instead of guessing at
  it.** `win_probability` used full-slate projections throughout, so points
  already on the board were ignored: live, it read 93% while the roster trailed
  20.3 to 57.82 after a Thursday night in which the opponent's quarterback
  scored 38.82. It now reports **63.6%** for the same matchup.

  A player whose game has kicked off carries his **actual** points and **no
  remaining variance** — which is the part that matters, because a settled
  lead cannot be lost to variance the way a projected one can. His slot leaves
  the optimization, since it can no longer be refilled, and a bench player
  whose game has started is no longer offered. Games in progress blend banked
  points with the untouched share of the projection.

  `nfl_mcp/game_clock.py` derives this from kickoff plus a nominal game length;
  the feeds carry no live clock. That is exact at both ends and approximate
  only during the ~3h window in between — when those lineups are locked anyway.
  An unknown kickoff counts as *not started*, deliberately: keeping a player in
  the optimizer is the recoverable error, freezing a changeable lineup is not.

  `optimize_win_probability` takes a `locked_players` argument for this, and
  optimizes the open slots against a residual target — `P(locked + open > opp)`
  is `P(open > opp - locked)` when the locked share has zero variance.

- **The briefing reports `points_so_far` and `opponent_points_so_far`**, plus
  an explicit `win_probability_basis`. `win_probability` is computed from
  full-slate projections and does **not** subtract points already scored, so a
  lopsided Thursday night reads as a comfortable lead when it is the opposite —
  live, 95.8% while trailing 20.3 to 57.82. Surfacing the actual score makes
  that visible instead of leaving it to be inferred.

- **An uncertainty-calibration eval** (`evals/backtest/calibration.py`, Layer A):
  floor/ceiling coverage plus win-probability calibration (Brier score and a
  reliability table) against real nflverse outcomes, leak-free and walk-forward,
  using the live constants. Win probability has no historical league matchups to
  test against, so matchups are synthesised within a week from a fixed seed.
  Runs in the scheduled evals workflow; offline guards run on every PR.

### Fixed

- **Projections ignored the league's scoring and were always full PPR.**
  `scoring` reached only the FantasyCalc *value* lookup; the points scale was
  hard-wired — `opportunity.py` set `REC = 1.0` and `base_ppg()` was documented
  as "Baseline PPR points/game". Nothing converted, so from week 2 on (the
  opportunity baseline) the projection was byte-identical for `ppr`, `half_ppr`
  and `standard`. A 0.5-PPR league was quoted full-PPR numbers: a receiver on
  9 targets / 6 receptions / 70 yards projected **14.6** where he is worth
  **≈11.6**.

  Worse than the points: it removed the ordering half PPR exists to create. A
  volume receiver outranked a runner in every format, which is exactly the FLEX
  call the setting is supposed to flip.

  Both baselines are now rebased to the league's per-reception value — the
  opportunity model takes `ppr` through to the reception weight *and* its
  per-target prior (rebased by position catch rate), and the rank buckets are
  scaled by the share of a full-PPR baseline that is reception bonus.
  `get_weekly_briefing` reads the exact value out of the league's own
  `scoring_settings` rather than rounding it to a three-way label, so a 0.6-PPR
  league is no longer projected as 0.5. `get_opportunity_projections` takes a
  `scoring` argument, and every projection response now reports the `scoring`
  and `ppr` it used.

- **The start/sit tools answered in full PPR off the weaker baseline.**
  `lineup_optimizer_tools` was the only place in the codebase that called the
  projection engine with no `scoring`, `season` or `week`. Everything else
  threads them, so after the scoring fix two tools in the same server disagreed
  about the same player: `get_start_sit_recommendation` said **17.7** where
  `get_weekly_briefing` said **14.9** for a 0.5-PPR league — a 19% gap, with
  start/sit on the wrong side of it.

  `week` made this hard to notice. `get_roster_recommendations` and
  `analyze_full_lineup` accepted it, validated it, echoed it back in the
  response — and never passed it to `analyze_player`, so the opportunity
  baseline (the better model, per the backtest) could not engage. The docstrings
  said "Optional NFL week number", which reads like it does something.

  All four tools (`get_start_sit_recommendation`, `get_roster_recommendations`,
  `compare_players_for_slot`, `analyze_full_lineup`) now take `scoring`,
  `season` and `week` and thread them through. Two follow-on repairs: the
  per-position "good game" marks were full-PPR constants applied to a
  now-format-dependent scale, which demoted every pass catcher in a half-PPR
  league by a confidence step, and are rebased the same way `base_ppg` is; and
  the single-player response dropped `floor`/`ceiling` entirely, leaving only a
  points string — so the calibrated band was invisible exactly where a start/sit
  call needs it.

- **Floor, ceiling and win probability claimed more certainty than they had.**
  `floor/ceiling = mean ± volatility·mean` is a ±1σ band under the Normal the
  lineup optimizer assumes, so reality should land inside 68.3% of the time.
  Measured over 2023–24 (n≈5.2k player-weeks) it landed inside **35.9%**, and
  the stated floor was breached **35.5%** of the time rather than 16% — the
  floor was not a floor. The win probability inherited it: matchups called 96%
  were won 79% of the time (Brier 0.2348 against a 0.2498 base-rate baseline).

  `_VOLATILITY` is now set per position to the width that actually covers 68.3%
  (QB 0.22→0.54, RB 0.30→0.65, WR 0.38→0.72, TE 0.40→0.74; K/DST carry the
  overall ≈2× correction, since they are not in the sample). Re-measured:
  coverage **68.5%**, tails 14.6% / 16.9%, Brier **0.2137**, and the best sd
  scale moves from 2.0 to 1.25 — i.e. about right. Accuracy is untouched (MAE
  5.823); what changed is that the uncertainty is now honest.

- **`get_scheme_classification` asserted a hand-maintained table as current
  fact.** Schemes were keyed by *team*, with no date and no fallback marker —
  the one place in this codebase that presented curated data with the
  confidence of a live fetch. Roughly a quarter of the league changes
  coordinator every offseason, so the table silently became last regime's
  answer: it still had Baltimore on the Roman-era power-run offense and Detroit
  on "McVay Offense" long after those staffs moved on.

  A scheme belongs to the play-caller, not the franchise, so it is now keyed by
  **coach** and the coach is resolved live through `get_coaching_staff`
  (coordinator first, then head coach). A staff change is picked up
  automatically, without editing a table every January. Checked live against
  Baltimore, whose current head coach the old table had never heard of.

  When no scheme is on file for the resolved coach it falls back to the team
  table — and says so, per side: `source: "team_table"`, `is_fallback: true`,
  `as_of`, and a warning naming the coach it could not match. `get_coaching_tree`
  likewise carries `as_of` and now states that lineage is *history*, not current
  employment, and that `found: false` means "not in this curated list of six
  major lineages" rather than "this coach has no lineage".

- **Playoff odds gave every team the same scoring spread.** `score_sd = 25.0`
  was hard-coded for the whole league, and that constant is what decides how
  often the weaker team wins — so a boom/bust roster and a metronome at equal
  points-per-game came out with identical odds. The means were computed from
  real results; only the variance was assumed.

  Each team's spread is now measured from its own played weeks and shrunk toward
  the league's pooled spread (about four games of league-average evidence mixed
  in), so a two-week sample cannot claim a roster is steady. The pooling is done
  around each *team's* mean rather than the league's, which keeps it a measure of
  week-to-week volatility rather than of how unequal the league is. `score_sd`
  survives as an explicit override.

  Every team's `score_sd` and `games_scored` are reported alongside its
  probability, plus `score_sd_source` (`measured` / `default` / `caller`), so a
  surprising number can be traced to the spread behind it and a thin sample is
  visible as one. Early in a season this correctly reports `default`.

- **Two injury queries filtered *after* the row limit and silently lost data.**
  `get_injury_trends(direction="worse")` fetched the newest N rows and then kept
  the downgrades, so a window that opens with a bulk feed backfill returned
  nothing — real: 3241 first sightings landed in a single day, against a limit
  capped at 500. The weekly briefing had the same shape, pulling 500 league-wide
  changes before narrowing to one roster's ~15 players. Both filters now run in
  SQL, with `LIMIT` applied last; `get_injury_status_changes` takes `player_ids`
  and `direction` (with the severity vocabulary passed in, so the database layer
  does not acquire an opinion about injuries).

- **`get_weekly_briefing` recommended starting a player who is out injured.**
  The briefing filtered Sleeper's `reserve` list, but a hurt player parked on
  the *active* roster — the normal state when the single IR slot is already
  occupied — was passed to the projection with no injury information at all.
  `_injury_mult` maps `IR`/`Out`/`PUP`/`Suspended` to `0.0` and would have
  zeroed him, but it never saw the status. Seen live: a receiver on IR with an
  ankle sprain won a FLEX slot. The status now travels with the player.

## [0.8.1] - 2026-09-17

A defect-hunting release. Every entry under *Fixed* is a bug that produced
plausible-looking output rather than an error — wrong implied totals, empty
handcuff maps, lineups containing players who cannot be started. Several were
found only because a previous fix made the next layer's silence visible.

Three shapes recur, and each now has a structural guard rather than a
one-off patch: a mapping duplicated until the copies drifted, a `.get(k, {})`
default that does not cover an explicit `null`, and an exception handler that
skips an item without logging.

### Added
- **`get_weekly_briefing` — the whole "how should I line up this week" question
  in one call.** Answering it previously meant chaining six calls and joining
  the results by hand: rosters, matchups, league settings, schedule, weather,
  trailing usage, projections, then the optimizer. That join is exactly where
  week boundaries and team-code variants slip in — both bugs fixed earlier in
  this release were found while doing it manually.

  The tool reads league scoring and starting slots from Sleeper rather than
  assuming them (a half-PPR league was otherwise given full-PPR advice),
  resolves opponents from the cached schedule rather than the odds feed, and
  returns the *named changes* worth making instead of a lineup to diff by eye.
  It also reports the roster's injury transitions from the last seven days,
  using the `injury_history` timeline added earlier in this release.

  Team defenses are listed under `not_projected` rather than counted as zero:
  there is no DST projection model yet, and a nameless 0-point candidate would
  quietly drag every lineup total down.

  Accepts `roster_id` or `user_id`; passing neither is refused rather than
  guessed.
- **`get_weekly_briefing` reports `vegas_active`.** A transient odds-API
  failure silently degrades defenses and kickers to their constant and flattens
  every game-script signal. The flag makes that visible instead of leaving it
  to be inferred from suspiciously round numbers.
- **Defenses and kickers are projected instead of returning a constant.**
  `base_ppg` gave every DST 7.0 and every kicker 8.0 regardless of opponent,
  and the generic path then scaled the defense by *its own* team's implied
  total — so a defense on a high-scoring team was rewarded, which has the
  causality backwards. A defense scores when the **opponent** does not.

  `defense_base()` prices a DST off the opponent's implied total (11.0 against
  a team implied at 16 or less, down to 3.5 against 28+), and `kicker_base()`
  off the kicker's own team total with a deliberate flattening at the top,
  since a team expected to score 30 trades field goals for touchdowns. Both
  fall back to the previous constant when Vegas lines are unavailable rather
  than inventing a number, and the projection now reports
  `opponent_implied_total` alongside the team's own.

  This closes the last structural gap in the model: lineup totals were
  understated by roughly 8–10 points per team, and `get_win_probability_lineup`
  could only compare 9 of 10 slots. `get_weekly_briefing` now returns a
  complete lineup with an empty `not_projected`.

  Measured on week 2: SF (facing a team implied at 16.0) projects 11.0 while
  TEN (facing 23.2) projects 6.5 — a 4.5-point spread where the old code
  returned 7.0 for both.

### Changed
- **The injury prefetch now delegates to `injury_service` instead of carrying
  its own copy of the ESPN crawl.** `sleeper_enrichment._fetch_injuries` walked
  32 teams and every injury detail sequentially; `injury_service` has done the
  same work concurrently for a while, with semaphores over both teams and
  injury details, an athlete-name cache, ETag/If-Modified-Since handling and a
  CBS merge on top. Measured against the live API, the duplicate needed **589s
  for 1600 single-source records** where the service needs **44s for 1906
  multi-source ones**. A full prefetch cycle drops from 418s to **47s**, which
  matters against a 900s interval that also has to fit schedules, snaps, usage
  and practice reports.

  This removes 169 lines and, more to the point, the second implementation:
  having two copies of one crawl is what allowed the v0.8.0 athlete-id bug to
  sit undetected in the unused one while the maintained one stayed correct. The
  end-to-end regression test moved with the parsing, onto `injury_service`.

  `_fetch_injuries` keeps its name, signature and return shape, so both callers
  (the prefetch loop and `_fetch_practice_reports`, which derives practice
  status from the same feed) are unaffected. It passes no `db`, because the
  callers own persistence — letting the service cache too would double-write.
- **One canonical team mapping, applied at the source.** Sleeper's athlete rows
  say `WAS` and `OAK` where the odds feed and ESPN say `WSH` and `LV`, and
  nflverse says `LA` for the Rams. Three separate normalizers existed —
  `VegasLinesAnalyzer._normalize_team`, `matchup_tools._NFLVERSE_TEAM_FIX` and
  `MatchupAnalyzer._normalize_team_name` — and the `athletes` table was covered
  by none of them. That is not a cosmetic inconsistency: an unnormalized code
  does not raise, it simply fails to join, which is how a healthy quarterback
  showed up as being on a bye.

  `nfl_mcp/teams.py` now owns `CANONICAL_TEAMS`, `TEAM_ALIASES` and
  `normalize_team()`; the existing normalizers delegate to it, and both athlete
  writers canonicalize `team_id` on insert so the stored data is consistent
  rather than only the readers that remember to convert. After one
  `fetch_athletes`, 84 `WAS` rows and 1 `OAK` row became `WSH` and `LV`.

  Ambiguous partial names are now refused instead of guessed: "New York"
  matches two teams, and the previous implementation returned whichever came
  first in dict order.

### Fixed
- **Vegas lines mixed two weeks, so half the league resolved to the wrong
  game.** The sportsbook publishes the next slate mid-week, and
  `fetch_current_lines` indexed each team with a plain assignment
  (`lines[home_team] = ...`), so whichever game came last in the feed won.
  Measured live in week 2: **15 of 32 teams resolved to their week-3 game**,
  with implied totals off by up to 3.4 points and the matchup tier computed
  against an entirely different opponent.

  This was not cosmetic. `projections.py` reads this index through
  `get_game_lines()`, so `project_player`, `project_players`,
  `get_game_environment`, `analyze_roster_vegas`, `get_stack_opportunities` and
  everything downstream — start/sit, win probability, the draft board — were
  quietly scoring players against the wrong game. Nothing errored; the numbers
  simply looked plausible. It also worsened as the week progressed, being
  correct only before the book posted the following week.

  Games are now sorted by kickoff and the per-team index keeps the *earliest*
  upcoming game via `setdefault`, which is what every consumer means by "that
  team's game". Each game additionally carries the NFL `week`, resolved from
  the cached schedule (`get_kickoff_week_index`), and `get_vegas_lines` accepts
  a `week` filter. Games whose week cannot be resolved are kept rather than
  dropped, so a cold schedule cache degrades to the old behaviour instead of
  returning an empty slate.
- **`get_weekly_briefing` reported a team defense as a lineup change every
  week, even when it was already starting.** Current starters were resolved via
  `full_name`, which is empty for defenses, while the candidate side named them
  after their team code — so the two sides of the comparison never matched.
  Both now use the same resolution. Caught against a live roster where the
  defense was correctly in the lineup and still appeared under `changes`.
- **The Vegas lookup in `projections` swallowed every exception silently.** A
  payload-shape change would have sent every player to the neutral fallback
  with no trace in the logs — the same failure mode that hid the injury fetcher
  returning zero records for months. It logs a warning now.
- **A fourth hand-rolled team normalizer in `get_matchup_difficulty`** handled
  only `WAS` and `JAC`; `LA`, `STL`, `OAK`, `SD` and every full name fell
  through to a neutral matchup tier. It now calls `normalize_team`.
- **`get_coaching_staff` and `get_scheme_classification` failed for
  Washington.** Both lookup tables were keyed on `WAS` while the rest of the
  codebase emits the canonical `WSH`, so `get_coaching_staff("WSH")` sent
  the literal string to ESPN as a numeric team id and got HTTP 400, and the
  scheme lookup reported "not found". Both tables are canonical now and both
  entry points normalize their input.
- **Projections priced home-team players off the *opponent's* implied total
  when the caller used a non-canonical team code.** `get_game_lines`
  normalizes its lookup but returns the canonical spelling, so
  `game["home_team"] == team` failed whenever a caller passed Sleeper's
  `WAS`/`JAC` or nflverse's `LA` — and the code then read the away side.
  Reproduced live: `LA` yielded an implied total of 20.4 where `LAR` gave 27.5
  for the same player in the same game, with `vegas_active: true` reported
  either way. The comparison is now canonical on both sides.
- **`get_handcuff_map` never found a handcuff for anyone.** It matched the
  starter's name against each depth-chart row's `position` field — but
  `get_depth_chart` returns `{"position": "RB", "players": [starter,
  backup, ...]}`, where `position` is a position *label*. A label can never
  equal a player name, so the lookup always fell through to the
  "you_roster_a_backup" branch and reported no handcuff and
  `0 securable free-agent handcuffs` — for every roster, every time.

  The producer had moved to this shape while the consumer kept reading the
  older one (row keyed by starter name, `players` holding only backups). Both
  shapes are now handled, with the current one first. On a live roster the tool
  went from 0 findings to correctly mapping four running backs and surfacing
  two free handcuffs.
- **`get_weekly_briefing` could recommend starting a player on IR.** Reserve
  and taxi players were treated as ordinary lineup candidates, so whenever one
  out-projected a healthy bench player the tool proposed a lineup the league
  will not accept. Seen live: an IR running back (thumb surgery) placed in a
  FLEX slot. They are now excluded from the candidate pool and reported under a
  separate `reserve` key — they are on the roster deliberately, not a gap.
- **Three silent `except: continue` handlers in `sleeper_strategy`** dropped a
  team from the bye-week and playoff-schedule scans with no log line, so a
  total upstream outage returned an empty result that still reported success —
  the same failure mode that let the injury fetcher return zero records for
  months. They log a warning naming the team now. A test asserts structurally
  that neither module regains a handler whose body is only `pass`/`continue`.
- **`get_playoff_odds` could lose every team name at once.**
  `u.get("metadata", {}).get("team_name")` raises on Sleeper's explicit
  `"metadata": null` — the `{}` default covers a missing key, not a null. A
  single such user aborted the whole comprehension, and the surrounding bare
  `except: pass` hid it completely, so every team silently degraded to
  "Roster 1", "Roster 2". Guarded with `or {}`, and the handler now logs.
- **The draft board's handcuff bonus never fired.** `_handcuff_index` tested
  `if starter not in names` — a raw string compare between Sleeper draft
  metadata (`"James Cook"`) and an ESPN depth chart (`"James Cook III"`). Every
  player with a suffix, accent or punctuation failed the test and was skipped
  with no log line, so the index came back mostly empty, the `1.30` handcuff
  multiplier never applied and `handcuff_for` was always `None`. The asymmetry
  was the tell: `_norm_name` was applied to the backup but not to the starter.
  Both sides are normalized now, and an unresolvable starter is logged.
- **Dead starter-weighting in `analyze_trade`.** `_calculate_positional_needs`
  built a `starter_counts` map and then evaluated `starter_counts.get(pos, 0)`
  as a bare expression statement — computed, discarded, never read. Removed
  rather than wired up: weighting need by who currently starts would change
  trade recommendations, which is a feature decision rather than a fix.
- **Roster-strength calculations counted players who cannot play.**
  `players_enriched` mirrors `players`, which includes reserve (IR) and taxi.
  Three tools read it as if every entry were available:
  `recommend_faab_bid` derived your replacement value from it, so a stashed
  RB1 counted as a live starter, collapsed the computed upgrade to zero and
  emitted *"You're already strong at RB — this is depth, not an upgrade"* for
  exactly the roster that needs the replacement; `analyze_trade`'s positional
  need scoring counted IR bodies as depth; and `analyze_opponent` read an
  opponent with two backs on IR as deep at the position. A shared
  `sleeper_tools.active_enriched()` now filters them. Availability questions
  ("is he rostered") deliberately keep reading `players`, where an IR player
  *is* taken.

### Documentation
- **`AGENT.md` now documents all 78 tools.** It described 44 across seven
  categories while the server had grown to 78, so every newer family — draft,
  projections, lineup optimization, matchup/schedule/weather, Vegas, injuries,
  FAAB/handcuffs, CBS — was missing entirely, including tools an agent has no
  other narrative reference for. The nine new sections are **generated from the
  live signatures and docstrings** rather than written by hand, so parameter
  lists and defaults cannot drift from the code the way the old ones did.

  Also fixes counts that had gone stale (the Sleeper category claimed 18 tools
  and listed 17) and a duplicated section number (two sections numbered 3).
  Both documents now state **79**, counting `get_weekly_briefing` added in this
  release and `get_league_leaders` behind the `league_leaders` feature flag —
  which is why a naive count of the registry returns 78.

## [0.8.0] - 2026-09-16

The weekly-usage pipeline never actually ran. Four independent defects, each
enough on its own to lose the data, kept `player_usage_stats`,
`player_week_stats` and `injury_history` empty for the whole season while the
server reported itself healthy. This release fixes all four, activates the
injury timeline that shipped dormant in v11, and adds a tool to read it.

### Added
- **`injury_history` is now written, and readable through the new
  `get_injury_trends` tool.** The table and its `add_injury_history()` /
  `get_injury_history()` helpers shipped in migration v11 but nothing ever
  called the writer: 0 rows against 2621 in `player_injuries`.
  `upsert_injuries` now records a row on a player's first sighting and on every
  later status or body-part change — **only** on a change, because the prefetch
  loop re-sends the identical feed every 15 minutes and copying it each cycle
  would bury the timeline in duplicates.

  `get_injury_trends` reads that timeline rather than the current snapshot, so
  it answers "what moved since I last looked" instead of "who is hurt", which
  the existing tools already cover. Each change carries its `previous_status`,
  a `direction` (`worse` / `better` / `lateral` / `new`) and a `severity_delta`
  derived from the existing `STATUS_SEVERITY` scale, filterable by window, team
  and direction. A same-severity relabel (a changed body part on an unchanged
  status) counts as `lateral`, not as a move in either direction.
- **`scripts/backfill_usage.py`** for the weeks the prefetch loop cannot reach.
  Each cycle only fetches `week - 1`, so a server started mid-season never
  acquires the earlier weeks. The script walks a week range and upserts both
  tables from the same free Sleeper endpoint; re-running is safe.

### Fixed
- **The prefetch loop never ran from a `.env`-only config**, so
  `player_usage_stats` and `player_week_stats` stayed empty for the whole
  season. `server.py` imports the tool registry — and through it
  `sleeper_enrichment` — *before* it loads `.env`, so the module-level
  `ADVANCED_ENRICH_ENABLED = os.getenv(...)` was evaluated against an empty
  environment. `/health` read the variable live and cheerfully reported
  `advanced_enrich_enabled: true` while the loop read the stale constant and
  logged `Prefetch disabled`, which made the symptom hard to trust. The flag is
  now resolved lazily via `advanced_enrich_enabled()`; the module attribute
  still wins when set, so existing `monkeypatch.setattr` overrides keep working.
- **The injury prefetch returned zero records for every team.**
  `_fetch_injuries` extracted the ESPN athlete id with `r'/athletes/(\d+)/'`,
  which requires a trailing slash. The injury payload's `athlete.$ref` ends *at*
  the id followed by a query string
  (`.../athletes/4684527?lang=en&region=us`), so the pattern never matched and
  every record hit a `continue`. The loop spent ~7 minutes and 1919 HTTP
  requests per cycle producing nothing, and because that `continue` logged
  nothing the failure was invisible even at DEBUG — the cycle summary just read
  `Injuries: 0 rows`.

  `injury_service` already had the correct pattern, which is precisely why the
  on-demand path kept working while the prefetch silently did not. Both now
  share `injury_service.extract_athlete_id()` so the two cannot drift apart
  again, the unresolvable-id branch logs a warning instead of skipping
  silently, and athlete display names are cached across teams. Verified against
  the live API: **1600 records** where the previous implementation returned 0.
- **`get_waiver_log` crashed on any league with activity**, taking
  `get_waiver_wire_dashboard` down with it. Sleeper sends `"adds": null` for a
  pure drop rather than omitting the key, so `transaction.get('adds', {})`
  returned `None` — the default only applies to a *missing* key — and `.keys()`
  raised `'NoneType' object has no attribute 'keys'`. In one real 12-team
  league, 1 of 11 week-1 transactions had `adds: null` and 5 had `drops: null`.
  The dashboard surfaced it as `http_error`, and without a `round` argument it
  returned an empty log with `success: true`, so the failure read as "no waiver
  activity" rather than an error. All eight add/drop accesses are now guarded.
- **`air_yards` was always NULL.** The usage parser probed `rec_air_yds`
  (plural) and `air_yards`; Sleeper ships `rec_air_yd`. 249 of 343 week-1 rows
  carry the field, and none of them were being read. Both legacy spellings are
  still accepted.
- **`snap_pct` was always NULL in `player_week_stats`.** Sleeper publishes no
  percentage field, so the snaps parser stored nothing and the response
  validator warned `Low snap_pct coverage: 0.0%` on every fetch. It is now
  derived from `off_snp / tm_off_snp` — the same calculation the usage fetcher
  already did — which is a measured value rather than the depth-chart guess
  from `_estimate_snap_pct`. Field probing moved to a `_first_present()` helper
  so a legitimate `0` survives, which plain `or` chaining discarded.
- **The scheduled Evals workflow failed every Tuesday** with
  `Unknown config option: asyncio_mode` (exit 4). The metric-test step installed
  bare `pytest`, but `pyproject.toml` sets `asyncio_mode = "auto"`, which only
  parses with `pytest-asyncio` present. Because Evals is scheduled and
  non-blocking, nobody saw it.

### Documentation
- `README.md` now covers all 77 tools; four were missing
  (`get_handcuff_map`, `get_injury_trends`, `get_opportunity_projections`,
  `get_win_probability_lineup`). The claim that AGENT.md carries every tool's
  full parameter reference was inaccurate — 35 of 77 are absent — and now points
  at the self-describing MCP schemas instead.
- `AGENT.md` documented prefetch as "set `NFL_MCP_PREFETCH=1`". Both that flag
  **and** `NFL_MCP_ADVANCED_ENRICH=1` are required; with only the first the loop
  starts and immediately returns. This is what kept the tables empty in
  practice, so it is a fix in its own right.

## [0.7.7] - 2026-09-13

### Added
- **Draft picks are now priced on injury, playoff schedule and handcuff status**
  (`recommend_draft_pick`). Ranking was purely positional, so a player on IR and
  a healthy one at the same ADP were indistinguishable on the clock. Four
  signals feed the valuation: injury multipliers for statuses that actually cost
  games (Out `.35`, IR `.30`, PUP `.45`, Suspended `.50`, Doubtful `.65`), a
  playoff-ease tilt that breaks ties on the week 15–17 schedule, a handcuff
  index flagging the backup to a RB already rostered, and unrankable-gap
  detection for positions with no viable target left before the next pick.

  `Questionable` deliberately scores `1.0`. In this feed it is a news channel,
  not a severity grade: a live preseason board tagged Mahomes ("on track to
  start Week 1"), Kraft ("expected to be full go"), Jeanty and a planned
  McCaffrey rest day all at identical `severity 2` / `confidence 65`, two of
  them mislabelled `Knee - ACL`. Discounting on that would push first-round
  talent down the board for nothing, so the note is surfaced to the drafter
  instead. Name matching normalises accents, suffixes and punctuation because
  the injury feed keys on ESPN ids while the value layer keys on Sleeper ids.
- **`.env` is loaded automatically** so `ODDS_API_KEY` no longer has to be
  exported by hand. Without it a local run silently fell back to a
  positional-rank prior with no game script at all (`vegas_active: false`).
  The loader is dependency-free, runs before the module-level `getenv` calls,
  resolves relative to the repo root rather than the cwd, and **never
  overwrites an already-set variable** — container and CI values still win.
  `.env.example` documents the variables; `.env` stays gitignored.

### Fixed
- **Vegas tools treated in-play lines as forecasts.** A sportsbook switches to
  in-play pricing at kickoff, where the total includes points *already on the
  board*. Week 1 surfaced a live CHI@CAR at a total of **79.5** and BAL@IND at a
  spread of **17.6**, and `get_stack_opportunities` duly ranked those halftime
  scores as the week's best shootouts. `fetch_current_lines` now skips started
  games by default; `include_live=True` keeps them, flagged with `is_live`.
  Excluded teams fall through to the existing neutral `is_fallback` defaults, so
  a live game reads as "no usable line" instead of a bogus total. An
  `include_live` result bypasses the cache in both directions — an in-play
  snapshot is valid for seconds, not the 2h TTL. Unparseable timestamps count as
  not-started so an upstream format change degrades to the previous behaviour
  rather than silently dropping every game.
- **Server host/port are configurable and a busy port fails loudly**
  (`NFL_MCP_HOST` / `NFL_MCP_PORT`). A local run alongside a containerised
  instance silently lost the bind race; the port is now probed up front and the
  process exits with a message naming the occupied address.
- **`get_cbs_projections` returned zero projections for every position.** Three
  compounding breaks, all reproduced against the live CBS page (the projections
  sibling of the 0.7.6 `get_cbs_expert_picks` rewrite):
  - The table selector matched `stats|data|projections`, but CBS renders the
    grid as `TableBase-table` — so no table was ever found and the tool reported
    `success: true` with an empty list. Now matches `TableBase` and falls back to
    the page's only table, so a further rename degrades visibly instead of
    silently.
  - CBS uses a **two-row `thead`** (group spans `Rushing`/`Receiving`/`Misc`
    above the real column labels). Flattening both misaligned every column, so
    values would have been labelled `Rushing` instead of `Games Played`. Only
    the last header row is used now, with CBS's concatenated abbreviation
    stripped (`ydsRushing Yards` → `Rushing Yards`, keeping rushing and
    receiving yards distinct).
  - The first body cell leads with a **text-less logo anchor**, so `find('a')`
    yielded an empty name and every row was dropped — this alone silently
    discarded all 32 DST rows. The first *labelled* anchor is used now.

  Live result: RB 100, QB 69, WR 100, TE 100, K 33, DST 32.

### Changed
- **`get_cbs_projections` now labels its granularity honestly.** CBS serves
  identical full-season numbers for `/1/`, `/2/` and `/restofseason/` — the week
  segment is ignored server-side. The payload carries `period: "season"`,
  `week_honoured: false` and an explanatory `note`; `week` is still validated
  and echoed back. Season totals must not be read as week-level projections.

## [0.7.6] - 2026-08-07

### Fixed
The two deeper audit follow-ups (deferred from the 0.7.5 batches):
- **`get_league_leaders` now returns actual leaders.** ESPN's `leaders` endpoint
  returns a *flat* list of leader entries (each with an `athlete` `$ref`), but the
  parser treated each entry as a *group* and expanded it to `[]` — so even
  completed seasons came back empty. It now uses the entries directly (deref
  athlete/team, ordered rank), e.g. 2024 passing → Burrow / Goff / Mayfield.
- **`get_cbs_expert_picks` is parsed properly.** Rewrote the scraper against the
  `TableExpertPicks` layout: clean expert names from the header row, per-game
  `picks` keyed by expert, extracted `away_team`/`home_team`, and
  whitespace-collapsed text — instead of concatenated blobs like
  `"FINALNBCDAL7-9-120PHI…"` and duplicated experts.

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
