"""Offline guards for the agent eval (Layer C) — no network / no API key.

These validate the durable parts (scenario set, schema builder, registry) so a
broken scenario or a tool losing its description is caught in normal CI, even
though the live LLM run is gated behind ANTHROPIC_API_KEY.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.agent import tools as T
from evals.agent.scenarios import SCENARIOS


def test_schema_builder_covers_all_tools():
    defs = T.anthropic_tools_from_registry()
    assert len(defs) >= 30
    for d in defs:
        assert d["name"]
        assert d["description"], f"{d['name']} has no description (hurts routing)"
        assert d["input_schema"]["type"] == "object"
        assert "properties" in d["input_schema"]


def test_scenarios_well_formed():
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "duplicate scenario ids"
    for s in SCENARIOS:
        assert s["prompt"].strip()
        assert s["expect"], f"{s['id']} has no expected tools"


def test_expected_tools_exist_in_registry():
    names = T.registry_tool_names()
    for s in SCENARIOS:
        for tool in s["expect"]:
            assert tool in names, f"scenario {s['id']} expects unknown tool '{tool}'"


def test_scenario_args_are_real_params():
    defs = {d["name"]: d for d in T.anthropic_tools_from_registry()}
    for s in SCENARIOS:
        for key in (s.get("args") or {}):
            in_some = any(key in defs[t]["input_schema"]["properties"] for t in s["expect"])
            assert in_some, f"scenario {s['id']} asserts arg '{key}' not on any expected tool"


def test_required_params_detected():
    defs = {d["name"]: d for d in T.anthropic_tools_from_registry()}
    # sanity: a tool with a required-first-arg is captured
    assert "draft_id" in defs["recommend_draft_pick"]["input_schema"]["required"]
    assert "league_id" in defs["get_playoff_odds"]["input_schema"]["required"]


def test_transactions_docstring_warns_about_pending_claims():
    """An agent must not read an empty transaction list as "no claims pending".

    Sleeper exposes a waiver claim only after processing, so pending claims are
    invisible here. That inference produced a confident "you have no claims
    pending" while two were waiting in the league app.
    """
    from nfl_mcp import sleeper_transactions, tool_registry

    for fn in (tool_registry.get_transactions, sleeper_transactions.get_transactions):
        doc = (fn.__doc__ or "")
        assert "pending" in doc.lower(), f"{fn.__name__} does not mention pending claims"
        assert "not" in doc.lower()
