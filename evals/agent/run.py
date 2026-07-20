"""
Agent tool-routing eval (Eval Layer C).

For each scenario we ask the model (with our MCP tool schemas attached) to answer
a user prompt, and check that it chooses an acceptable tool with sensible args.
This is single-turn routing — we inspect what the model *wants* to call; we don't
execute the tools. That's the highest-signal, lowest-cost slice of Layer C.

Requires ANTHROPIC_API_KEY. Without it the run is skipped (exit 0) so it degrades
gracefully; with it, the process exits non-zero if the pass rate drops below the
threshold (regression signal).

Run: python -m evals.agent.run   [--model claude-sonnet-5] [--threshold 0.8]
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

from .scenarios import SCENARIOS
from .tools import anthropic_tools_from_registry

SYSTEM = (
    "You are a fantasy football assistant with tools for an NFL analytics server. "
    "When the user asks something a tool can answer, call the most appropriate tool "
    "with the arguments you can infer from the message. Prefer a tool over a plain "
    "text answer whenever one fits."
)

DEFAULT_MODEL = os.getenv("AGENT_EVAL_MODEL", "claude-sonnet-5")


def _tool_calls(resp) -> List[Any]:
    return [b for b in resp.content if getattr(b, "type", None) == "tool_use"]


def _check_args(call_input: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    """Return a list of arg problems (empty = all good)."""
    problems = []
    for key, want in (expected or {}).items():
        if key not in call_input or call_input[key] in (None, ""):
            problems.append(f"missing arg '{key}'")
        elif want is not None and str(call_input[key]) != str(want):
            problems.append(f"{key}={call_input[key]!r} != {want!r}")
    return problems


def run(model: str = DEFAULT_MODEL) -> List[Dict[str, Any]]:
    import anthropic  # lazy: only needed for the live run

    client = anthropic.Anthropic()
    tools = anthropic_tools_from_registry()
    results = []
    for sc in SCENARIOS:
        entry = {"id": sc["id"], "expect": sc["expect"]}
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                system=SYSTEM,
                tools=tools,
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": sc["prompt"]}],
            )
            calls = _tool_calls(resp)
            called = [c.name for c in calls]
            entry["called"] = called
            hit = next((c for c in calls if c.name in sc["expect"]), None)
            if not hit:
                entry.update(ok=False, reason=f"called {called or 'nothing'}; expected one of {sc['expect']}")
            else:
                problems = _check_args(hit.input or {}, sc.get("args"))
                entry.update(ok=not problems, reason="; ".join(problems) if problems else "ok")
        except Exception as e:  # noqa: BLE001
            entry.update(ok=False, called=[], reason=f"error: {type(e).__name__}: {e}")
        results.append(entry)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent tool-routing eval (Layer C)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--threshold", type=float, default=0.8, help="min pass rate")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("SKIPPED: ANTHROPIC_API_KEY not set — agent eval needs it to call the model.")
        print("Set the key (or the ANTHROPIC_API_KEY repo secret) to run this eval.")
        return 0

    print("=" * 78)
    print(f"AGENT TOOL-ROUTING EVAL — model={args.model}, {len(SCENARIOS)} scenarios")
    print("=" * 78)
    results = run(args.model)
    passed = 0
    for r in results:
        icon = "✅" if r["ok"] else "❌"
        print(f"  {icon} {r['id']:<20} -> {', '.join(r.get('called') or ['-']):<28} [{r['reason']}]")
        passed += 1 if r["ok"] else 0
    rate = passed / len(results) if results else 0.0
    print("-" * 78)
    print(f"  pass rate {passed}/{len(results)} = {rate:.0%} (threshold {args.threshold:.0%})")
    print("=" * 78)
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
