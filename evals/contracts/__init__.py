"""Live data-source contract checks (Eval Layer B).

Early-warning system: hit each upstream data source and assert that the fields
and shapes our code depends on still exist. This is exactly what would have
caught the ESPN/FantasyPros defense-rankings breakage the day it happened.

Run: ``python -m evals.contracts.checks`` (exits non-zero if a CRITICAL check
fails). Runs on a schedule via ``.github/workflows/contracts.yml``.
"""
