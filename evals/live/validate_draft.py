"""
Pre-draft flight check — validate the whole Sleeper draft flow against a REAL
draft, before draft day.

Drives the actual code paths (sleeper_tools.get_draft / get_draft_picks and
draft_tools.recommend_draft_pick) so any mismatch between our parsing and the live
Sleeper API surfaces now, not while you're on the clock.

Usage (any one of):
    python -m evals.live.validate_draft --draft-id 998877
    python -m evals.live.validate_draft --league-id 555         # newest draft
    python -m evals.live.validate_draft --username your_name --season 2026
Optionally: --my-slot 3
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from typing import List, Optional

from nfl_mcp import sleeper_tools as st, draft_tools as dt
from nfl_mcp.database import NFLDatabase

_results: List[dict] = []


def _ok(name: str, detail: str):
    _results.append({"name": name, "ok": True, "detail": detail})
    print(f"  ✅ {name:<26} {detail}")


def _fail(name: str, detail: str):
    _results.append({"name": name, "ok": False, "detail": detail})
    print(f"  ❌ {name:<26} {detail}")


async def _resolve_draft_id(args) -> Optional[str]:
    if args.draft_id:
        return args.draft_id
    if args.league_id:
        r = await st.get_league_drafts(args.league_id)
        drafts = r.get("drafts", []) if r.get("success") else []
        if drafts:
            _ok("resolve.league_drafts", f"{len(drafts)} draft(s); using {drafts[0].get('draft_id')}")
            return drafts[0].get("draft_id")
        _fail("resolve.league_drafts", f"no drafts for league {args.league_id}: {r.get('error')}")
        return None
    if args.username:
        u = await st.get_user(args.username)
        if not (u.get("success") and u.get("user")):
            _fail("resolve.user", f"username '{args.username}' not found")
            return None
        uid = u["user"].get("user_id")
        _ok("resolve.user", f"{args.username} -> {uid}")
        for season in (args.season, args.season - 1):
            lr = await st.get_user_leagues(uid, season)
            for lg in (lr.get("leagues", []) if lr.get("success") else []):
                dr = await st.get_league_drafts(lg.get("league_id"))
                drafts = dr.get("drafts", []) if dr.get("success") else []
                if drafts:
                    _ok("resolve.leagues", f"{lg.get('name')} ({season}) -> draft {drafts[0].get('draft_id')}")
                    return drafts[0].get("draft_id")
        _fail("resolve.leagues", "no leagues with a draft found for recent seasons")
    return None


async def run(args) -> int:
    print("=" * 78)
    print("PRE-DRAFT FLIGHT CHECK")
    print("=" * 78)
    draft_id = await _resolve_draft_id(args)
    if not draft_id:
        return 1

    db = NFLDatabase(tempfile.mktemp(suffix=".db"))

    # 1) get_draft — settings & format parsing
    d = await st.get_draft(draft_id)
    if not (d.get("success") and d.get("draft")):
        _fail("get_draft", f"{d.get('error')}")
        return 1
    draft = d["draft"]
    settings = draft.get("settings", {}) or {}
    reqs = dt._starter_requirements(settings)
    scoring = dt._scoring_from_draft(draft)
    _ok("get_draft", f"status={draft.get('status')} teams={settings.get('teams')} "
                     f"scoring={scoring} starters={reqs}")

    # 2) get_draft_picks — shape & player ids
    p = await st.get_draft_picks(draft_id)
    picks = p.get("picks", []) if p.get("success") else []
    if not p.get("success"):
        _fail("get_draft_picks", f"{p.get('error')}")
        return 1
    if picks:
        pk = picks[0]
        pos = (pk.get("metadata") or {}).get("position")
        if pk.get("player_id") and pk.get("draft_slot"):
            _ok("get_draft_picks", f"{len(picks)} picks; sample pid={pk.get('player_id')} slot={pk.get('draft_slot')} pos={pos}")
        else:
            _fail("get_draft_picks", f"pick shape unexpected: keys={sorted(pk.keys())}")
    else:
        _ok("get_draft_picks", "0 picks (draft not started yet — fine pre-draft)")

    # 3) recommend_draft_pick — end-to-end (values + roster need)
    r = await dt.recommend_draft_pick(draft_id, my_slot=args.my_slot, num_suggestions=3, db=db)
    if not r.get("success"):
        _fail("recommend_draft_pick", f"{r.get('error')}")
        return 1
    sugg = r.get("suggestions", [])
    if sugg:
        top = ", ".join(f"{x['name']}({x['position']})" for x in sugg)
        _ok("recommend_draft_pick", f"picks_made={r.get('picks_made')} format={r.get('format', {}).get('scoring')} "
                                    f"source={r.get('source')} | top: {top}")
    else:
        _ok("recommend_draft_pick", f"picks_made={r.get('picks_made')} (no players left / pool exhausted)")

    print("-" * 78)
    ok = sum(1 for x in _results if x["ok"])
    crit_fail = sum(1 for x in _results if not x["ok"])
    print(f"  {ok}/{len(_results)} checks passed, {crit_fail} failure(s)")
    print("=" * 78)
    return 1 if crit_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the Sleeper draft flow against a real draft")
    ap.add_argument("--draft-id")
    ap.add_argument("--league-id")
    ap.add_argument("--username")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--my-slot", type=int, default=1)
    args = ap.parse_args()
    if not (args.draft_id or args.league_id or args.username):
        ap.error("provide one of --draft-id / --league-id / --username")
    return asyncio.run(run(args))


if __name__ == "__main__":
    import sys
    sys.exit(main())
