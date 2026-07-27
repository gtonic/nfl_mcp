"""project_player now uses the opportunity baseline when season+week are given."""
import types

from nfl_mcp import opportunity_tools
from nfl_mcp.opportunity import project_opportunity
from nfl_mcp.projections import ProjectionEngine


def _engine():
    """A ProjectionEngine with stubbed (offline) value/defense/vegas deps."""
    eng = ProjectionEngine.__new__(ProjectionEngine)
    eng.values = types.SimpleNamespace(lookup=lambda *a, **k: None)  # no market
    eng.defense = types.SimpleNamespace(
        get_matchup_difficulty=lambda *a, **k: {"matchup_tier": "neutral"}
    )
    eng.vegas = types.SimpleNamespace(get_game_lines=lambda *a, **k: {"is_fallback": True})
    return eng


def _wr_games():
    return [
        {"week": w, "targets": 8, "receptions": 6, "receiving_yards": 80,
         "receiving_tds": 0, "carries": 0}
        for w in range(1, 5)
    ]


def _index():
    logs = {"x1": {"player_id": "x1", "name": "Test Receiver", "position": "WR",
                   "team": "BUF", "games": _wr_games()}}
    return opportunity_tools.build_name_index(logs)


def test_opportunity_base_used_when_season_week_given():
    eng = _engine()
    player = {"name": "Test Receiver", "position": "WR", "team": "BUF", "opponent": "MIA",
              "usage": {"snap_percentage": 90, "usage_trend": "up"}}
    # neutral matchup + fallback vegas + no injury -> multipliers are 1.0, so the
    # projection equals the opportunity base (usage is intentionally skipped).
    out = eng._project_one(player, {}, {}, {}, _index(), week=6)

    expected = round(project_opportunity(_wr_games(), "WR"), 1)
    assert out["breakdown"]["base_source"] == "opportunity"
    assert out["value_source"] == "opportunity"
    assert out["breakdown"]["usage_mult"] == 1.0        # skipped to avoid double-count
    assert out["breakdown"]["base_ppg"] == expected
    assert out["projected_points"] == expected


def test_falls_back_to_rank_bucket_without_opportunity_data():
    eng = _engine()
    player = {"name": "Unknown Guy", "position": "WR", "team": "BUF", "opponent": "MIA",
              "usage": {"snap_percentage": 90, "usage_trend": "up"}}
    # No opp_index -> rank-bucket baseline and usage multiplier applies as before.
    out = eng._project_one(player, {}, {}, {}, {}, week=6)
    assert out["breakdown"]["base_source"] == "rank_bucket"
    assert out["value_source"] == "baseline"
    assert out["breakdown"]["usage_mult"] != 1.0        # usage trend "up" applied


def test_name_normalization_matches_suffix_and_punctuation():
    idx = opportunity_tools.build_name_index(
        {"a": {"player_id": "a", "name": "A.J. Brown Jr.", "position": "WR",
               "team": "PHI", "games": _wr_games()}}
    )
    # Query without the punctuation/suffix should still resolve.
    assert opportunity_tools.opportunity_base_for(idx, "AJ Brown", "WR", week=6) is not None
