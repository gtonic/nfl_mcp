# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/gtonic/nfl_mcp/compare/v0.5.16...HEAD
[0.5.16]: https://github.com/gtonic/nfl_mcp/releases/tag/v0.5.16
