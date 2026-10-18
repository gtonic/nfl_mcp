"""Snap risk: a player back from an injury whose snaps are in doubt.

Ladd McConkey, 2026 week 6: active in week 5 with 0 offensive snaps (foot),
no designation, LP-LP-FP, news "limited snaps / pitch count". The projection
had 8.7 with a calibrated floor of 2.8 and start/sit preferred him over a
stable tight end without a word about it.
"""
from unittest.mock import AsyncMock

import pytest

from nfl_mcp import lineup_optimizer_tools as lo
from nfl_mcp import opportunity_tools, projections

pytestmark = pytest.mark.usefixtures("offline_sources")


def _line(off, team=67, active=1.0):
    row = {"tm_off_snp": float(team), "gms_active": active}
    if off:
        row["off_snp"] = float(off)
    return row


# His Sleeper weekly lines (week 5: active, no snaps; week 4: 34%).
LADD = {1: {"11635": _line(27, 55)}, 2: {"11635": _line(31, 68)}, 3: {"11635": _line(61, 69)},
        4: {"11635": _line(24, 71)}, 5: {"11635": _line(0)}}


class TestActiveWithoutSnaps:
    def test_the_mcconkey_week(self):
        assert projections.active_without_snaps(LADD, "11635") == {"week": 5, "snap_share": 0.0}

    def test_a_normal_last_game_is_no_risk(self):
        stats = {4: {"1": _line(50)}, 5: {"1": _line(45)}}
        assert projections.active_without_snaps(stats, "1") is None

    def test_inactive_last_game_is_an_absence_not_a_snap_question(self):
        stats = {4: {"1": _line(50)}, 5: {"1": _line(0, active=0.0)}}
        assert projections.active_without_snaps(stats, "1") is None

    def test_a_bye_looks_back_to_the_last_game(self):
        stats = {4: {"1": _line(50)}, 5: {"1": _line(3)}, 6: {}}
        assert projections.active_without_snaps(stats, "1")["week"] == 5

    def test_a_backup_who_never_plays_offense_is_no_snap_question(self):
        stats = {3: {"1": _line(2)}, 4: {"1": _line(5)}, 5: {"1": _line(0)}}
        assert projections.active_without_snaps(stats, "1") is None

    def test_unknown_player(self):
        assert projections.active_without_snaps(LADD, None) is None
        assert projections.active_without_snaps({}, "11635") is None


def _proj(**extra):
    return {"player": "Ladd McConkey", "position": "WR", "projected_points": 8.7,
            "floor": 2.8, "ceiling": 14.6, "confidence": 72, "breakdown": {},
            "news_flags": [], **extra}


class TestApplySnapRisk:
    def test_zero_snap_game_cuts_the_mean_and_lowers_the_floor(self):
        p = _proj(breakdown={"active_no_snaps": {"week": 5, "snap_share": 0.0}})
        projections._apply_snap_risk([p])
        mean = round(8.7 * projections.SNAP_RISK_MEAN_MULT, 1)
        assert p["projected_points"] == mean == 8.1
        assert p["floor"] == round(mean * (1 - 0.68 * projections.SNAP_RISK_FLOOR_WIDEN), 1)
        assert p["floor"] < 1.5 and p["ceiling"] == round(mean * 1.68, 1)
        assert p["confidence"] == 67
        assert "week 5" in p["snap_risk"]["reasons"][0]

    def test_news_alone_lowers_the_floor_only(self):
        """Our share already carries limited_snaps' multiplier (news_signals)."""
        p = _proj(news_flags=[{"flag": "limited_snaps", "weight": 0.71}])
        projections._apply_snap_risk([p])
        assert p["projected_points"] == 8.7
        assert p["floor"] < 2.8 and p["ceiling"] == 14.6
        assert p["snap_risk"]["mean_mult"] == 1.0

    def test_stale_news_and_healthy_players_are_untouched(self):
        stale = _proj(news_flags=[{"flag": "limited_snaps", "weight": 0.2}])
        plain = _proj()
        projections._apply_snap_risk([stale, plain])
        for p in (stale, plain):
            assert p["projected_points"] == 8.7 and p["floor"] == 2.8 and "snap_risk" not in p

    def test_bye_and_inactive_are_left_alone(self):
        bye = _proj(on_bye=True, projected_points=0.0, floor=0.0, ceiling=0.0,
                    breakdown={"active_no_snaps": {"week": 5, "snap_share": 0.0}})
        out = _proj(gameday_status="inactive", projected_points=0.0,
                    breakdown={"active_no_snaps": {"week": 5, "snap_share": 0.0}})
        projections._apply_snap_risk([bye, out])
        assert "snap_risk" not in bye and "snap_risk" not in out


@pytest.mark.asyncio
async def test_role_reads_carry_the_zero_snap_game(monkeypatch):
    logs = {"l": {"player_id": "l", "name": "Ladd McConkey", "position": "WR", "team": "LAC",
                  "games": [{"week": w, "team": "LAC", "opponent": "X", "targets": 5.0}
                            for w in (1, 2, 3, 4)]}}

    async def week_stats(_season, wk):
        return LADD.get(wk, {})
    monkeypatch.setattr(projections.usage_trends, "_fetch_week_stats", week_stats)
    monkeypatch.setattr(projections.role_shift, "player_rows", lambda *a, **k: [])
    reads, _ = await projections._role_reads(
        [{"name": "Ladd McConkey", "position": "WR", "team": "LAC", "player_id": "11635"}],
        logs, opportunity_tools.build_name_index(logs), 2026, 6)
    assert reads[0]["active_no_snaps"] == {"week": 5, "snap_share": 0.0}


class TestCompare:
    @pytest.mark.asyncio
    async def test_the_verdict_names_the_snap_risk(self, monkeypatch):
        risk = {"reasons": ["active in week 5 with 0% of the snaps"], "mean_mult": 0.93,
                "floor_widen": 1.3}
        rows = {"Ladd McConkey": {"projected_points": 8.1, "floor": 0.9, "ceiling": 13.6,
                                  "snap_risk": risk},
                "Mark Andrews": {"projected_points": 7.4, "floor": 2.1, "ceiling": 12.7}}

        class Engine:
            async def project_many(self, players, **_k):
                name = players[0]["name"]
                return {"projections": [{"player": name, "breakdown": {}, **rows[name]}]}

        monkeypatch.setattr(projections, "get_projection_engine", lambda db=None: Engine())
        from nfl_mcp.matchup_tools import DefenseRankingsAnalyzer
        analyzer = DefenseRankingsAnalyzer(db=None)
        analyzer.fetch_defense_rankings = AsyncMock(return_value={})
        analyzer.get_matchup_difficulty = lambda *a, **k: {"rank": 16, "matchup_tier": "neutral"}
        optimizer = lo.LineupOptimizer(db=None, defense_analyzer=analyzer)
        monkeypatch.setattr(optimizer, "_second_opinion", AsyncMock())
        monkeypatch.setattr(lo, "get_lineup_optimizer", lambda: optimizer)
        players = [{"name": "Ladd McConkey", "position": "WR", "team": "LAC", "opponent": "KC",
                    "injury": {"status": "healthy", "practice_status": "FP"}},
                   {"name": "Mark Andrews", "position": "TE", "team": "BAL", "opponent": "CLE",
                    "injury": {"status": "healthy", "practice_status": "FP"}}]
        out = await lo.compare_players_for_slot(players, slot="FLEX", risk_mode="neutral",
                                                season=2026, week=6)
        assert out["winner"]["player"] == "Ladd McConkey"
        assert "snap risk on Ladd McConkey" in out["verdict"]
        assert "floor 0.9 vs Mark Andrews's 2.1" in out["verdict"]
        ladd = next(r for r in out["comparison"] if r["player"] == "Ladd McConkey")
        assert ladd["snap_risk"] == risk
        assert ladd["reasoning"][0].startswith("⚠️ Snap risk")
        andrews = next(r for r in out["comparison"] if r["player"] == "Mark Andrews")
        assert "snap_risk" not in andrews
