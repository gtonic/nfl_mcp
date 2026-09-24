# Evals

This directory holds **evaluations** — how we know the NFL MCP server's analytics
are actually *good*, not just plausible-looking. Evals turn hand-tuned heuristics
into **measured, tunable** models.

## The three eval layers

An analytics-heavy MCP has three distinct things worth evaluating. They live in
different places and run on different cadences.

| Layer | Question | Where | Cadence |
|-------|----------|-------|---------|
| **A. Analytical accuracy** | Do our outputs predict reality? | `evals/backtest/` | scheduled / on-demand |
| **B. Contract checks** | Do the data sources & tools still return the schema we depend on? | `tests/` (offline) + `evals/contracts/` (live, scheduled) | PR + scheduled |
| **C. Agent / tool-use** | Does an LLM *use* the tools correctly and safely? | `evals/agent/` | on tool-description changes |

> **All three layers are implemented.** Layer C's live run is gated behind an
> API key; its offline guards run in normal CI.

Why the split? The fast unit tests (`tests/`, PR-blocking) prove the code *runs*.
Evals prove the code is *right about football* — which needs real historical data
and is too slow/networked to block every PR.

---

## Layer A — projection accuracy backtest (`evals/backtest/`)

### What it answers
> Do the projection engine's adjustments (matchup, usage) actually make weekly
> point projections **more accurate** than a sensible baseline — and are their
> magnitudes tuned right?

The projection engine (`nfl_mcp/projections.py`) computes:

```
projected = base_ppg × matchup_mult × environment_mult × usage_mult × injury_mult
```

The multipliers were hand-picked (matchup ±10%, environment ±8%, …). This
backtest measures whether they help, using **real outcomes**.

### Method (walk-forward, leak-free)
For every player-week in the test range we predict that week's PPR points using
**only information available before the week**:

| Model | Formula |
|-------|---------|
| `base` | the player's trailing average PPR (prior weeks only) |
| `matchup` | `base × matchup_mult(opponent defense vs position)` — defense ranking computed from **prior weeks only** |
| `usage` | `base × usage_mult(recent touch trend)` |
| `full` | `base × matchup_mult × usage_mult` |

Ground truth = the player's **actual** PPR points that week, from
[nflverse](https://github.com/nflverse/nflverse-data) (the same source the live
server uses for defense rankings — so backtest and production agree on reality).

The multipliers are **imported from the live engine**
(`nfl_mcp.projections._MATCHUP_MULT`, `_usage_mult`, `matchup_tools._get_matchup_tier`),
so this literally evaluates production's constants. Change the constant → the
eval moves.

> **Leakage matters.** Defense rankings for week *W* use only weeks `< W`;
> trailing PPG uses only prior games. Nothing from week *W* (or the future) leaks
> into a prediction for week *W*.

### Metrics (`metrics.py`, pure stdlib)
- **MAE** — mean absolute error, in fantasy points (lower is better). "How far off, on average."
- **RMSE** — like MAE but punishes big misses more.
- **Spearman** — rank correlation (higher is better). *Did we order players like reality did?* This is the metric that matters most for start/sit.
- **bias** — mean signed error (pred − actual). >0 over-predicts, <0 under-predicts.
- **R²** — variance explained.

### Run it
```bash
# from the repo root (needs nfl_mcp importable, e.g. after `pip install -e .`)
python -m evals.backtest.backtest --seasons 2024 --start-week 5 --min-trailing 5

# options
--seasons 2023,2024     # combine seasons
--start-week 5          # first test week (needs enough prior weeks)
--min-prior 3           # min prior games for a stable trailing average
--min-trailing 5.0      # only score fantasy-relevant players (avg ≥ 5 PPR)
--positions RB,WR       # restrict positions
```
The nflverse CSV is downloaded once and cached under `evals/backtest/.cache/`
(gitignored).

### How to read the output
- The **`full vs base`** line is the headline: does adding the multipliers reduce MAE?
- The **per-position** table shows where adjustments help vs hurt.
- The **matchup-strength tuning** sweeps a scalar `s` (effective multiplier
  `= 1 + s·(mult−1)`) and reports the `s` that minimises MAE:
  - `s ≈ 1` → the live magnitudes are about right.
  - `s < 1` → we **over-adjust**; soften the multipliers.
  - `s > 1` → we **under-adjust**; strengthen them.

---

## Findings (2024, n ≈ 2,500 player-weeks)

```
base      MAE=5.78  Spearman=0.472
matchup   MAE=5.79  Spearman=0.468
usage     MAE=5.78  Spearman=0.478
full      MAE=5.79  Spearman=0.474
=> full vs base: MAE 5.78 -> 5.79 (-0.2%)  [adjustments do NOT help on aggregate]

Per position (base -> full MAE):
  QB:  6.60 -> 6.68  (-1.2%)   RB: 5.73 -> 5.68 (+0.9%)
  WR:  5.77 -> 5.82  (-0.9%)   TE: 5.09 -> 5.02 (+1.2%)

Matchup-strength tuning: best s ≈ 0.5  => we OVER-adjust.
```

**What this tells us (and what to do):**

1. **Weekly fantasy scoring is high-variance.** A good base (trailing PPG) already
   gets MAE ≈ 5.8 pts and Spearman ≈ 0.47; the adjustments move things by
   *fractions of a point*. Matchup is a small edge, not a magic wand — set
   expectations accordingly in the UI.
2. **Our flat ±10% matchup multiplier is too aggressive** on aggregate (best
   strength ≈ 0.5, i.e. ≈ ±5%).
3. **Matchup should be position-specific:** it helps **RB and TE** (scheme /
   game-script dependent) but *hurts* **QB and WR** (talent dominates, defenses
   matter less week-to-week).
4. **The usage trend adjustment is roughly neutral** on aggregate.

### ✅ Loop closed — the finding was applied and re-measured

The matchup multiplier is now **position-specific** (`matchup_multiplier()` in
`nfl_mcp/projections.py`), with per-position strength tuned by the backtest's
per-position sweep on 2023–24:

| Position | strength (× the ±10% swing) | why |
|---|---|---|
| RB | **1.0** (full) | matchup matters most (scheme / game script) |
| TE | 0.5 | moderate |
| QB | 0.25 | small |
| WR | **0.0** (off) | talent dominates; matchup was pure noise |

Re-running the same backtest confirms the fix:

```
             flat ±10% (before)      position-specific (after)
full vs base   5.842 -> 5.853  (-0.2%, HURTS)   5.842 -> 5.835  (+0.1%, HELPS)
matchup-only          — (net negative)          5.833  (< base 5.842, better Spearman)
per position   QB -1.2% WR -0.9%  (hurt)         QB +0.2% RB +0.5% TE +0.3% WR ~0
```

So the adjustment now **helps instead of hurts**, and no longer penalises QBs/WRs.
The effect is still small in absolute terms (weekly scoring is noisy) — matchup is
a real but modest edge. This measure → change → re-measure loop is exactly what
Layer A is for.

### Limitations / honesty
- **Base differs from production.** Here the base is trailing actual PPG (available
  historically); the live engine's base is a positional-rank baseline until
  in-season usage enrichment kicks in. The backtest validates the **multipliers**,
  which transfer; it also *suggests* the live engine should prefer trailing PPG as
  its base once enough weeks exist.
- **No historical Vegas or market values** for free, so `environment_mult` and a
  value-based base aren't backtested yet. (`environment_mult` is only active in
  production when `ODDS_API_KEY` is set.)
- **One season shown.** Add `--seasons 2022,2023,2024` for a larger sample before
  making changes.
- Small effects mean you need a decent sample (`n`) before trusting a delta.

---

## Layer A — uncertainty calibration (`evals/backtest/calibration.py`)

### What it answers
> The projection reports a **floor** and a **ceiling**, and
> `get_win_probability_lineup` turns them into *"you have a 72% chance to win"*.
> Do those numbers mean what they say?

Two properties, measured separately because they fail differently:

1. **Band coverage.** `floor/ceiling = mean ± volatility·mean` is a symmetric ±1σ
   band under the Normal the optimizer assumes, so reality should land inside
   **68.3%** of the time with **15.9%** in each tail.
2. **Win-probability calibration.** Of the matchups called 70%, do ~70% get won?
   Reported as a **Brier score** plus a reliability table. A model can rank
   perfectly and still be badly calibrated — and miscalibration is not cosmetic
   here: the optimizer tilts toward *ceiling* when it thinks you are the
   underdog, so a wrong probability picks a wrong lineup.

### Method
Same leak-free walk-forward as the accuracy backtest: projections from the live
opportunity baseline on prior weeks only, `sd` from the live `_VOLATILITY` via
`win_probability.player_sd` — production's constants, not a copy.

There are no historical league matchups to test win probability against, so
matchups are **synthesised**: within a week, players are shuffled with a fixed
seed into two lineups of nine, P(win) comes from the projections, and the actual
points decide it. Both sides face the same week, so nothing systematic separates
them. Weeks are re-partitioned 5× — that buys precision on the curve, not
independent samples.

```bash
python -m evals.backtest.calibration --seasons 2023,2024 --start-week 5
python -m evals.backtest.calibration --seasons 2024 --ppr 0.5   # half-PPR league
```

### Findings (2023–24, n ≈ 5.2k player-weeks, 1385 synthetic matchups)

The hand-picked volatilities were **about half as wide as reality**:

```
inside the band   35.9%   (claims 68.3%)
below the floor   35.5%   (claims 15.9%)   <- the floor was not a floor
z: mean=0.14 sd=2.40      (honest uncertainty => mean 0, sd 1)

Win probability   Brier 0.2348  (base-rate baseline 0.2498)
  called 96% -> won 79%      badly over-confident at both extremes
  called  4% -> won 19%
```

### ✅ Loop closed — applied and re-measured

`_VOLATILITY` is now set per position to the width that actually covers 68.3%
(QB 0.22→0.54, RB 0.30→0.65, WR 0.38→0.72, TE 0.40→0.74; K/DST carry the overall
≈2× correction since they are not in the nflverse player-stats sample):

```
                   before            after
inside             35.9%             68.5%     (want 68.3%)
tails         35.5% / 28.6%     14.6% / 16.9%  (want 15.9% each)
z_sd               2.40              1.18
Brier             0.2348            0.2137
called 90-100% -> 79% won        -> 88.5% won
best sd scale       2.0              1.25       (i.e. ~right)
```

> **Read the floor as a floor now, not before.** This did not make the
> projections more accurate — MAE is untouched — it made the *stated
> uncertainty* honest, which is what the win probability and the floor/ceiling
> tilt are built on.

### Limitations
- Synthetic matchups are random lineups, not real rosters; a real league has
  correlated, self-selected teams. The calibration direction is trustworthy, the
  third decimal of the Brier is not.
- K/DST volatility is inferred, not measured.
- Coverage is measured on fantasy-relevant players (`--min-trailing 5`); deep
  bench players are noisier still.

### Sleeper-first blend (`sleeper_blend.py`)
The weekly projection is `0.25 × our model + 0.75 × Sleeper`. This eval
reproduces the weight: production's model (regressed opportunity × matchup ×
Vegas) vs Sleeper's historical weekly projections vs the blend, a sweep of the
model weight, band coverage at the live `_VOLATILITY` and start/sit pairwise
accuracy. Truth everywhere in `evals/backtest` is the stat line priced by
`ScoringModel` (Sleeper defaults), not nflverse's `fantasy_points_ppr`.

```bash
python -m evals.backtest.sleeper_blend --seasons 2023 2024 2025   # network on first run
python -m evals.backtest.sleeper_blend --include-dnp              # + weeks the player sat
```
Sleeper history is cached (compacted, ~0.6 MB/week) in `evals/backtest/.cache/sleeper/`.

---

## Layer B — data-source contract checks (`evals/contracts/`)

An **early-warning system.** Every day, hit each upstream source and assert the
fields our code depends on still exist. This is exactly what would have caught the
ESPN/FantasyPros defense-rankings breakage the day it happened, instead of it
silently degrading to placeholder data.

Where possible a check drives our *own* code (so it also catches our parsing
breaking); the field-level checks hit the raw API so a failure pinpoints an
*upstream* change.

| Check | Level | Asserts |
|-------|-------|---------|
| `fantasycalc.values` | critical | list of values; `player.sleeperId`, `position`, `value`, `positionRank`; and our value service returns them |
| `nflverse.defense_rankings` | critical | 32 teams × QB/RB/WR/TE, computed via our analyzer, `source == nflverse` |
| `nflverse.usage_columns` | critical | player_stats CSV parses with `fantasy_points_ppr` + targets/carries |
| `sleeper.state` | critical | `week` / `season` / `season_type` |
| `sleeper.week_stats` | critical | `off_snp` / `tm_off_snp` / `rec_tgt` present (snap%/usage enrichment) |
| `sleeper.players` | warn | big players map; entries have `position` + name |
| `espn.teams` | warn | ≥32 teams with `abbreviation` |
| `espn.news` | warn | articles with headlines |

Checks auto-fall-back to the most recent season that has published data, so they
stay green year-round.

```bash
python -m evals.contracts.checks     # exits non-zero iff a CRITICAL check fails
```

Runs daily via `.github/workflows/contracts.yml` (+ manual dispatch). A **critical**
failure turns the job red and GitHub notifies you; **warn** failures are reported
but don't fail the job. Offline runner tests live in `tests/test_eval_contracts.py`.

---

## Layer C — agent / tool-use evals (`evals/agent/`)

Does an LLM using our tools pick the **right tool** for a request? A wrong tool
(or bad args) means a wrong answer no matter how good the analytics are — and
tool *descriptions* are what drive that choice, so this guards them.

- **Scenarios** (`scenarios.py`): realistic prompts → the tool(s) a good assistant
  should call (any-of), plus optional argument assertions.
- **Tool schemas** (`tools.py`): derived from the live `tool_registry` (name +
  docstring + signature), so the eval sees exactly what production exposes. Pure
  and offline.
- **Runner** (`run.py`): single-turn routing — attach the tool schemas, ask the
  model each prompt, and check it *chose* an acceptable tool with sensible args
  (we inspect the `tool_use`, we don't execute the tool). Highest signal, lowest
  cost.

```bash
export ANTHROPIC_API_KEY=...            # required for the live run
python -m evals.agent.run --model claude-sonnet-5 --threshold 0.8
# no key -> skips gracefully (exit 0)
```

Runs **on demand** via `.github/workflows/agent-evals.yml` (needs the
`ANTHROPIC_API_KEY` repo secret; costs tokens). The **offline guards** —
scenario/schema/registry validity and "every tool still has a description" — run
in the normal test suite (`tests/test_agent_scenarios.py`), so tool-description
regressions are caught on every PR without a key.

> Not yet run against the live model in this repo (no key configured here). Add
> the `ANTHROPIC_API_KEY` secret and dispatch the workflow to get routing numbers.

---

## Roadmap

- ~~Apply the finding: position-aware matchup multipliers, then re-measure.~~ ✅ done.
- ~~Layer B — live source contract checks.~~ ✅ done (`evals/contracts/`).
- ~~Layer C — agent tool-routing evals.~~ ✅ done (`evals/agent/`).
- ~~Win-probability **calibration** (Brier score) and floor/ceiling coverage.~~
  ✅ done (`evals/backtest/calibration.py`); the finding was applied to
  `_VOLATILITY` and re-measured.
- **More Layer A targets:** backtest start/sit hit-rate, defense-ranking
  predictive validity (split-sample), FAAB bid ↔ realized value, and playoff-odds
  calibration against real league outcomes once multi-season snapshots exist.
- **Deeper Layer C:** faithfulness/safety judging (execute the tool, then check
  the rendered answer doesn't present fallback/stale/unknown data as confident) —
  needs a multi-turn loop + LLM-as-judge.

## CI
The backtest runs in a **scheduled, non-PR-blocking** workflow
(`.github/workflows/evals.yml`) and can be triggered manually
("Run workflow"). It prints the report to the job log so you can track accuracy
over time without slowing down PRs.
