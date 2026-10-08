"""ROS later weeks blended with Sleeper's projection for that week (offline)."""
from datetime import timedelta

import pytest

from nfl_mcp import projections, ros
from nfl_mcp import sleeper_projections as sp
from tests.test_ros_projections import SETTINGS, FakeDB, _player, _stub_projection


def _row(name, team="BUF", position="WR", rec=None, rec_yd=None, pid=None):
    stats = {}
    if rec is not None:
        stats = {"pts_ppr": 1.0, "rec": rec, "rec_yd": rec_yd or 0.0}
    return {"player_id": pid or name, "team": team,
            "player": {"first_name": name, "last_name": None, "position": position},
            "stats": stats}


def _index(*rows):
    # A teammate with points, so a pointless row is an explicit zero.
    return sp._index([*rows, _row("Teammate", rec=1, rec_yd=10)])


def _sleeper(monkeypatch, by_week: dict[int, dict]):
    async def _weeks(season, weeks):
        return {w: by_week.get(w, {"by_id": {}, "by_name": {}, "by_def": {}}) for w in weeks}
    monkeypatch.setattr(ros, "_sleeper_weeks", _weeks)


async def _run(monkeypatch, players, points=None, breakdown=None, extra=None, week=3):
    _stub_projection(monkeypatch, points or {}, breakdown)
    if extra:
        orig = projections.project_players

        async def _with_extra(ps, **kw):
            res = await orig(ps, **kw)
            for p in res["projections"]:
                p.update(extra)
            return res
        monkeypatch.setattr(projections, "project_players", _with_extra)
    return await ros.ros_projections(players, season=2026, week=week, settings=SETTINGS,
                                     db=FakeDB(), include_weekly=True)


RATE = {"base_ppg": 10.0, "base_source": "rank_bucket"}


class TestLaterWeeksBlend:
    async def test_later_weeks_blend_ours_with_sleepers_week(self, monkeypatch):
        # 6 rec + 80 yds = 14.0 in PPR.
        _sleeper(monkeypatch, {w: _index(_row("Receiver", rec=6, rec_yd=80))
                               for w in range(4, 18)})
        out = await _run(monkeypatch, [_player("Receiver")], points={"Receiver": 10.0},
                         breakdown=RATE)
        p = out["players"][0]
        assert p["weekly_points"][3] == 10.0  # this week: the weekly projection
        assert p["weekly_points"][4] == pytest.approx(0.25 * 10.0 + 0.75 * 14.0)
        row = next(r for r in p["weekly"] if r["week"] == 4)
        assert row["source"] == "sleeper_blend"
        assert row["model_points"] == 10.0 and row["sleeper_points"] == 14.0
        assert p["ros_source"] == "sleeper_blend"
        assert out["sleeper_ros"]["active"] and out["sleeper_ros"]["weeks"][0] == 4

    async def test_week_without_sleeper_falls_back_to_model(self, monkeypatch):
        _sleeper(monkeypatch, {4: _index(_row("Receiver", rec=6, rec_yd=80))})
        out = await _run(monkeypatch, [_player("Receiver")], points={"Receiver": 10.0},
                         breakdown=RATE)
        p = out["players"][0]
        assert p["weekly_points"][5] == pytest.approx(10.0)
        assert next(r for r in p["weekly"] if r["week"] == 5)["source"] == "model"
        assert p["ros_source"] == "mixed"
        assert 5 in out["sleeper_ros"]["weeks_missing"]

    async def test_no_sleeper_at_all_is_the_model(self, monkeypatch):
        out = await _run(monkeypatch, [_player("Receiver")], points={"Receiver": 10.0},
                         breakdown=RATE)
        p = out["players"][0]
        assert p["weekly_points"][4] == pytest.approx(10.0)
        assert p["ros_source"] == "model"
        assert out["sleeper_ros"]["active"] is False

    async def test_hurt_player_stays_out_until_sleeper_projects_him(self, monkeypatch):
        weeks = {4: _index(_row("Hurt")), 5: _index(_row("Hurt"))}
        weeks.update({w: _index(_row("Hurt", rec=6, rec_yd=80)) for w in range(6, 18)})
        _sleeper(monkeypatch, weeks)
        out = await _run(monkeypatch, [_player("Hurt", status="Out")], points={"Hurt": 0.0},
                         breakdown=RATE)
        p = out["players"][0]
        # Our window: week 3. Sleeper's: through week 5.
        assert p["injury_weeks"] == [3, 4, 5]
        assert p["weekly_points"][4] == 0.0 and p["weekly_points"][5] == 0.0
        assert p["weekly_points"][6] == pytest.approx(13.0)
        assert p["sleeper_return_week"] == 6
        assert "Sleeper" in next(r for r in p["weekly"] if r["week"] == 4)["reason"]

    async def test_hurt_player_sleeper_never_projects_keeps_the_model(self, monkeypatch):
        _sleeper(monkeypatch, {w: _index(_row("Hurt")) for w in range(4, 18)})
        out = await _run(monkeypatch, [_player("Hurt", status="Out")], points={"Hurt": 0.0},
                         breakdown=RATE)
        p = out["players"][0]
        assert p["injury_weeks"] == [3]
        assert p["weekly_points"][4] == pytest.approx(10.0)
        assert next(r for r in p["weekly"] if r["week"] == 4)["source"] == "model"

    async def test_healthy_backup_listed_without_points_keeps_our_share(self, monkeypatch):
        _sleeper(monkeypatch, {w: _index(_row("Backup")) for w in range(4, 18)})
        out = await _run(monkeypatch, [_player("Backup")], points={"Backup": 10.0},
                         breakdown=RATE)
        p = out["players"][0]
        assert p["weekly_points"][4] == pytest.approx(2.5)
        assert p["injury_weeks"] == []

    async def test_role_multiplier_moves_only_sleepers_share(self, monkeypatch):
        _sleeper(monkeypatch, {w: _index(_row("Receiver", rec=6, rec_yd=80))
                               for w in range(4, 18)})
        out = await _run(monkeypatch, [_player("Receiver")], points={"Receiver": 10.0},
                         breakdown=RATE, extra={"role_multiplier": 0.5})
        p = out["players"][0]
        assert p["weekly_points"][4] == pytest.approx(0.25 * 10.0 + 0.75 * 7.0)

    async def test_backup_qb_cut_on_sleepers_share_for_the_starters_absence(self, monkeypatch):
        _sleeper(monkeypatch, {w: _index(_row("Receiver", rec=6, rec_yd=80))
                               for w in range(4, 18)})
        qb = {"applied": True, "model_mult": 0.8, "sleeper_mult": 0.5, "games_out": 2}
        out = await _run(monkeypatch, [_player("Receiver")], points={"Receiver": 10.0},
                         breakdown=RATE, extra={"qb_context": qb})
        p = out["players"][0]
        # Week 4 is the starter's second missed game: both shares cut.
        assert p["weekly_points"][4] == pytest.approx(0.25 * 8.0 + 0.75 * 7.0)
        # Week 5: he is back.
        assert p["weekly_points"][5] == pytest.approx(0.25 * 10.0 + 0.75 * 14.0)

    async def test_bye_stays_zero_whatever_sleeper_says(self, monkeypatch):
        _sleeper(monkeypatch, {w: _index(_row("Receiver", rec=6, rec_yd=80))
                               for w in range(4, 18)})
        _stub_projection(monkeypatch, {"Receiver": 10.0}, RATE)
        out = await ros.ros_projections([_player("Receiver")], season=2026, week=3,
                                        settings=SETTINGS, db=FakeDB({7: {"BUF"}}),
                                        include_weekly=True)
        p = out["players"][0]
        assert p["weekly_points"][7] == 0.0 and p["bye_weeks"] == [7]


class TestFetchWeeks:
    async def test_parallel_fetch_is_cached_per_week(self, monkeypatch):
        calls = []

        async def _fetch(season, week):
            calls.append(week)
            return sp._index([_row("Receiver", rec=6, rec_yd=80)])
        monkeypatch.setattr(sp, "_fetch", _fetch)
        first = await sp.fetch_weeks(2026, [4, 5, 6])
        assert sorted(first) == [4, 5, 6] and all(i["by_id"] for i in first.values())
        await sp.fetch_weeks(2026, [4, 5, 6])
        assert sorted(calls) == [4, 5, 6]
        # A stale copy (older than the TTL) is fetched again.
        sp._cache[(2026, 4)] = (sp._cache[(2026, 4)][0] - timedelta(hours=13),
                                sp._cache[(2026, 4)][1])
        await sp.fetch_weeks(2026, [4])
        assert calls.count(4) == 2

    async def test_empty_week_is_not_retried_at_once(self, monkeypatch):
        calls = []

        async def _fetch(season, week):
            calls.append(week)
            return {"by_id": {}, "by_name": {}, "by_def": {}}
        monkeypatch.setattr(sp, "_fetch", _fetch)
        await sp.fetch_weeks(2026, [9])
        await sp.fetch_weeks(2026, [9])
        assert calls == [9]

    async def test_a_failing_week_never_raises(self, monkeypatch):
        async def _fetch(season, week):
            raise RuntimeError("boom")
        monkeypatch.setattr(sp, "_fetch", _fetch)
        out = await sp.fetch_weeks(2026, [10, 11])
        assert out[10]["by_id"] == {} and out[11]["by_id"] == {}
