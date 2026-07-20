"""
Live data-source contract checks (Eval Layer B).

Each check hits a real upstream source (or drives our own code that does) and
asserts the fields we depend on still exist. A check "fails" by raising; the
runner turns that into a red result. CRITICAL failures make the process exit
non-zero so the scheduled workflow goes red and notifies.

Design notes
- Field-level checks hit the raw API so a failure pinpoints an *upstream* change.
- A couple of checks drive our own code paths (FantasyCalc via the value service,
  nflverse via the defense analyzer) so they also catch *our* parsing breaking.
- Everything is best-effort and isolated: one source being down doesn't abort the
  others.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, UTC
from typing import Callable, List, Tuple

import httpx

# (name, critical, fn)
CHECKS: List[Tuple[str, bool, Callable[[], str]]] = []


def check(name: str, critical: bool = True):
    def deco(fn: Callable[[], str]):
        CHECKS.append((name, critical, fn))
        return fn
    return deco


def _get(url: str, **kw) -> httpx.Response:
    r = httpx.get(url, follow_redirects=True, timeout=45, **kw)
    r.raise_for_status()
    return r


def _completed_seasons() -> List[int]:
    """Recent seasons that should have full published data (newest first)."""
    now = datetime.now(UTC)
    base = now.year - (1 if now.month >= 3 else 2)
    return [base, base - 1]


# ---------------------------------------------------------------------------
# FantasyCalc — powers player values, trades, draft board, projections base
# ---------------------------------------------------------------------------
@check("fantasycalc.values", critical=True)
def _fantasycalc() -> str:
    data = _get(
        "https://api.fantasycalc.com/values/current",
        params={"isDynasty": "false", "numQbs": 1, "numTeams": 12, "ppr": 1},
    ).json()
    assert isinstance(data, list) and len(data) > 100, f"expected a big list, got {type(data)} len={len(data) if hasattr(data,'__len__') else '?'}"
    first = data[0]
    player = first.get("player") or {}
    assert player.get("sleeperId"), "player.sleeperId missing (breaks Sleeper mapping)"
    assert player.get("position"), "player.position missing"
    assert first.get("value") is not None, "value missing"
    assert first.get("positionRank") is not None, "positionRank missing"
    # end-to-end through our service (catches our parsing too)
    from nfl_mcp.player_values import PlayerValuesService
    svc = PlayerValuesService(db=None)
    res = asyncio.run(svc.get_values(1.0, 1, 12, False))
    assert res.get("source") == "fantasycalc" and res.get("count", 0) > 100, \
        f"value service returned source={res.get('source')} count={res.get('count')}"
    return f"{len(data)} values; top={player.get('name')} (sleeperId ok)"


# ---------------------------------------------------------------------------
# nflverse — powers defense-vs-position rankings + backtest ground truth
# ---------------------------------------------------------------------------
@check("nflverse.defense_rankings", critical=True)
def _nflverse_defense() -> str:
    from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
    an = DefenseRankingsAnalyzer(db=None)
    for season in _completed_seasons():
        rankings = asyncio.run(an.fetch_defense_rankings(season))
        wr = rankings.get("WR", [])
        if wr and wr[0].get("source") == "nflverse":
            assert len(wr) == 32, f"expected 32 teams, got {len(wr)}"
            for pos in ("QB", "RB", "TE"):
                assert len(rankings.get(pos, [])) == 32, f"{pos} not 32 teams"
            return f"{season}: 32 teams × 4 positions from nflverse"
    raise AssertionError(f"no nflverse defense data for seasons {_completed_seasons()}")


@check("nflverse.usage_columns", critical=True)
def _nflverse_columns() -> str:
    from evals.backtest.data import load_season
    for season in _completed_seasons():
        try:
            recs = load_season(season)
        except Exception:
            continue  # that season's CSV not published yet -> try older
        if recs:
            assert any(r["ppr"] for r in recs), "no fantasy_points_ppr values"
            assert any(r["touches"] > 0 for r in recs), "no targets/carries (touches)"
            return f"{season}: {len(recs)} REG records with ppr + touches"
    raise AssertionError("nflverse player_stats unavailable for recent seasons")


# ---------------------------------------------------------------------------
# Sleeper — powers league/roster/matchups + usage & snap enrichment
# ---------------------------------------------------------------------------
@check("sleeper.state", critical=True)
def _sleeper_state() -> str:
    d = _get("https://api.sleeper.app/v1/state/nfl").json()
    for k in ("week", "season", "season_type"):
        assert k in d, f"state missing '{k}'"
    return f"week={d['week']} season={d['season']} ({d['season_type']})"


@check("sleeper.week_stats", critical=True)
def _sleeper_week_stats() -> str:
    """Snap% + targets enrichment depends on off_snp / tm_off_snp / rec_tgt."""
    for season in _completed_seasons():
        try:
            d = _get(f"https://api.sleeper.app/v1/stats/nfl/regular/{season}/1").json()
        except Exception:
            continue
        if not (isinstance(d, dict) and len(d) > 100):
            continue
        have_snaps = sum(1 for s in d.values() if isinstance(s, dict) and "off_snp" in s and "tm_off_snp" in s)
        have_tgt = sum(1 for s in d.values() if isinstance(s, dict) and "rec_tgt" in s)
        assert have_snaps > 50, f"only {have_snaps} players with off_snp/tm_off_snp"
        assert have_tgt > 50, f"only {have_tgt} players with rec_tgt"
        return f"{season} wk1: {have_snaps} w/ snaps, {have_tgt} w/ targets"
    raise AssertionError("no sleeper week stats for recent seasons")


@check("sleeper.players", critical=False)
def _sleeper_players() -> str:
    d = _get("https://api.sleeper.app/v1/players/nfl").json()
    assert isinstance(d, dict) and len(d) > 1000, "players map too small"
    sample = next((v for v in d.values() if isinstance(v, dict) and v.get("position")), None)
    assert sample and (sample.get("full_name") or sample.get("last_name")), "player entry shape changed"
    return f"{len(d)} players; sample has position + name"


# ---------------------------------------------------------------------------
# ESPN — powers news/teams/injuries/standings/schedules/coaching (warn-level)
# ---------------------------------------------------------------------------
@check("espn.teams", critical=False)
def _espn_teams() -> str:
    d = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams").json()
    teams = d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    assert len(teams) >= 32, f"expected ≥32 teams, got {len(teams)}"
    t0 = teams[0].get("team", {})
    assert t0.get("abbreviation"), "team.abbreviation missing"
    return f"{len(teams)} teams; sample={t0.get('abbreviation')}"


@check("espn.news", critical=False)
def _espn_news() -> str:
    d = _get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/news").json()
    arts = d.get("articles", [])
    assert arts and arts[0].get("headline"), "news articles/headline missing"
    return f"{len(arts)} articles"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_all() -> List[dict]:
    results = []
    for name, critical, fn in CHECKS:
        try:
            detail = fn()
            results.append({"name": name, "critical": critical, "ok": True, "detail": detail})
        except Exception as e:  # noqa: BLE001 - report any failure
            results.append({"name": name, "critical": critical, "ok": False, "detail": f"{type(e).__name__}: {e}"})
    return results


def exit_code(results: List[dict]) -> int:
    """Non-zero iff a CRITICAL check failed (warnings don't fail the job)."""
    return 1 if any((not r["ok"] and r["critical"]) for r in results) else 0


def main() -> int:
    print("=" * 78)
    print(f"DATA-SOURCE CONTRACT CHECKS — {datetime.now(UTC).isoformat(timespec='seconds')}")
    print("=" * 78)
    results = run_all()
    crit_fail = 0
    warn_fail = 0
    for r in results:
        icon = "✅" if r["ok"] else ("❌" if r["critical"] else "⚠️ ")
        tag = "CRIT" if r["critical"] else "warn"
        print(f"  {icon} [{tag}] {r['name']:<28} {r['detail']}")
        if not r["ok"]:
            if r["critical"]:
                crit_fail += 1
            else:
                warn_fail += 1
    print("-" * 78)
    ok = sum(1 for r in results if r["ok"])
    print(f"  {ok}/{len(results)} passed | {crit_fail} critical failure(s), {warn_fail} warning(s)")
    print("=" * 78)
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
