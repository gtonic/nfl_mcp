"""
Live draft "war room" — watch a real Sleeper draft and get a recommendation
each time you're on the clock.

This is the reusable version of the ad-hoc loop we used to validate the draft
flow live. Point it at a live/mock Sleeper draft and your slot; it polls the real
draft state, and when it's your turn it prints roster context + the top picks
(with value cliffs and positional runs). In the bench rounds it flips to a depth
overlay (RB/WR + handcuffs), because pure VBD goes blind on deep benches.

Usage:
    python -m evals.live.draft_watch --draft-id 1384996703937003520 --my-slot 4
    python -m evals.live.draft_watch --draft-id <id> --my-slot 4 --once   # single check
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from collections import Counter
from typing import Optional

from nfl_mcp import sleeper_tools as st, draft_tools as dt
from nfl_mcp.database import NFLDatabase

VBD_POS = ("QB", "RB", "WR", "TE")


def _starters_full(counts: dict, reqs: dict) -> bool:
    base_full = all(counts.get(p, 0) >= reqs.get(p, 0) for p in ("QB", "RB", "WR", "TE"))
    flex_full = dt._flex_filled(counts, reqs) >= reqs.get("FLEX", 0)
    return base_full and flex_full


async def _show_turn(db, draft_id: str, my_slot: int, teams: int, reqs: dict, picks, num: int):
    n = len(picks)
    mine = [pk for pk in picks if pk.get("draft_slot") == my_slot]
    counts = Counter((pk.get("metadata") or {}).get("position") for pk in mine)
    my_last = max((pk.get("pick_no", 0) for pk in mine), default=0)
    since = [pk for pk in picks if pk.get("pick_no", 0) > my_last]
    rnd = n // teams + 1

    print("\n" + "=" * 70)
    print(f">>> YOU'RE ON THE CLOCK — pick #{n + 1} (round {rnd}), slot {my_slot}")
    print("=" * 70)
    team = [f"{(pk.get('metadata') or {}).get('last_name','?')}"
            f"({(pk.get('metadata') or {}).get('position','?')})" for pk in mine]
    print("Your roster:", team or "(empty)")
    if since:
        print(f"Gone since your last pick ({len(since)}): "
              f"{dict(Counter((pk.get('metadata') or {}).get('position') for pk in since))}")

    r = await dt.recommend_draft_pick(draft_id, my_slot=my_slot, num_suggestions=num, db=db)
    if not r.get("success"):
        print("recommend error:", r.get("error"))
        return

    bench_mode = _starters_full(counts, reqs)
    if bench_mode:
        bp = r.get("best_available_by_position") or {}
        print("\nBENCH MODE — starters filled. Prioritise RB/WR depth + handcuffs;")
        print("ignore backup QB / extra TE (pure VBD is blind on deep benches).")
        for pos in ("RB", "WR"):
            if pos in bp:
                print(f"  best {pos} available: {bp[pos]['name']} (vbd {bp[pos]['vbd']})")
    else:
        print("\nTop picks (by need-weighted value):")
        for s in r.get("suggestions", []):
            print(f"  {s['name']:<22} {s['position']:<3} vbd={s['vbd']:<7} :: {'; '.join(s['reasoning'])}")
        if r.get("value_cliffs"):
            print("  value cliffs:", r["value_cliffs"])
    top = r.get("top_pick")
    if top:
        print(f"\n=> Suggested: {top['name']} ({top['position']})"
              + (" — but in bench mode, take the RB/WR body above" if bench_mode else ""))


async def _final(picks, my_slot):
    mine = [pk for pk in picks if pk.get("draft_slot") == my_slot]
    print("\n" + "=" * 70)
    print("DRAFT COMPLETE — your roster:")
    for pk in sorted(mine, key=lambda x: x.get("pick_no", 0)):
        m = pk.get("metadata") or {}
        print(f"  R{pk.get('round')}  {m.get('first_name','')} {m.get('last_name','')} ({m.get('position')})")
    print("=" * 70)


async def run(draft_id: str, my_slot: int, interval: int, num: int, once: bool):
    db = NFLDatabase(tempfile.mktemp(suffix=".db"))
    d = await st.get_draft(draft_id)
    if not (d.get("success") and d.get("draft")):
        print("Could not load draft:", d.get("error"))
        return 1
    s = d["draft"].get("settings", {}) or {}
    teams = int(s.get("teams", 12) or 12)
    rounds = int(s.get("rounds", 15) or 15)
    reqs = dt._starter_requirements(s)
    print(f"Watching draft {draft_id}: {teams}-team {dt._scoring_from_draft(d['draft'])} "
          f"snake, {rounds} rounds. You are slot {my_slot}. Starters: {reqs}")

    recommended_for = -1
    while True:
        p = await st.get_draft_picks(draft_id)
        picks = p.get("picks", []) if p.get("success") else []
        n = len(picks)
        if n >= teams * rounds:
            await _final(picks, my_slot)
            return 0
        on_clock = dt._snake_slot(n, teams)
        if on_clock == my_slot and n != recommended_for:
            await _show_turn(db, draft_id, my_slot, teams, reqs, picks, num)
            recommended_for = n
            if once:
                return 0
            print(f"\n(make your pick in Sleeper; watching for the next turn — Ctrl-C to stop)")
        await asyncio.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live Sleeper draft watcher")
    ap.add_argument("--draft-id", required=True)
    ap.add_argument("--my-slot", type=int, required=True)
    ap.add_argument("--interval", type=int, default=8, help="poll seconds")
    ap.add_argument("--num", type=int, default=6, help="suggestions to show")
    ap.add_argument("--once", action="store_true", help="single check, don't loop")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.draft_id, args.my_slot, args.interval, args.num, args.once))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
