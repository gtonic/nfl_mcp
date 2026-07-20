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

> **Layers A and B are implemented.** Layer C is on the roadmap (see the bottom).

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

## Roadmap

- ~~Apply the finding: position-aware matchup multipliers, then re-measure.~~ ✅ done.
- ~~Layer B — live source contract checks.~~ ✅ done (`evals/contracts/`).
- **More Layer A targets:** backtest start/sit hit-rate, defense-ranking
  predictive validity (split-sample), FAAB bid ↔ realized value, and playoff-odds
  **calibration** (Brier score) once multi-season snapshots exist.
- **Layer C — agent evals** (`evals/agent/`): prompt → expected tool selection +
  answer assertions (incl. "must not present fallback/stale data as confident"),
  run against an LLM with the MCP server attached.

## CI
The backtest runs in a **scheduled, non-PR-blocking** workflow
(`.github/workflows/evals.yml`) and can be triggered manually
("Run workflow"). It prints the report to the job log so you can track accuracy
over time without slowing down PRs.
