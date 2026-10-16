"""Check a multi-step roster plan before any of it is entered in Sleeper.

Two live failures motivated this. A claim was entered without a drop while the
roster was full, and Sleeper rejected it at the waiver run ("Unfortunately,
your roster will have too many players after this transaction"). And a claim
the user intended was never entered at all — the assistant took it as done,
and the player went to another team. Plans like "add X dropping Y now; claim Z
dropping W before Wednesday's run; after the game drop V for a DEF" have to
hold against several rules at once:

* roster size: the active roster (everything but IR/taxi) may not exceed the
  league's ``roster_positions`` count — Sleeper checks this when the claim
  *processes*, not when it is entered;
* IR: a move into IR needs a free reserve slot and a Sleeper status the league
  allows (`ir_audit`); a player sitting in IR who is no longer eligible blocks
  every add and claim until he is activated;
* waivers: a player whose game has kicked off (this week or last) or who was
  dropped recently is on waivers — he can only be claimed, and the claim lands
  at the next run (`waiver_rules.waiver_status`, the logic behind
  `get_player_ownership`); a free agent is an instant add;
* game locks: a player whose game has started cannot be dropped, added or moved
  in or out of the lineup until the week rolls over (estimated as the waiver-day
  run after the week's last game);
* byes and backups: what the roster can start in the next weeks, and whether a
  drop leaves a single-slot position (QB/TE/K/DEF) without anyone for a bye.

`simulate` is pure: it replays the steps in time order on a `PlanContext`
(rosters, settings, kickoffs, rest-of-season points) and reports each step as
valid or invalid with the reason, the resulting roster and lineups, warnings
and a checklist ordered by deadline. Invalid steps are not applied. Nothing is
ever entered in Sleeper — `verify_plan` compares a plan with what actually
happened afterwards, because a pending claim is invisible to the API and a
plan is not a transaction.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .game_clock import LOCAL_TZ, local_time, parse_kickoff
from .ir_audit import eligible_statuses, is_ir_eligible
from .lineup_slots import (
    SLOT_ELIGIBILITY,
    normalize_position,
    normalize_slot,
    optimal_lineup,
    slot_accepts,
    starting_slot_list,
)
from .waiver_rules import next_run, waiver_rules, waiver_status

logger = logging.getLogger(__name__)

ACTIONS = ("add", "drop", "claim", "ir_move", "activate", "start", "bench")
_ACTION_ALIASES = {
    "pickup": "add", "pick_up": "add", "free_agent": "add", "fa": "add",
    "waiver": "claim", "waiver_claim": "claim",
    "release": "drop", "cut": "drop",
    "ir": "ir_move", "move_to_ir": "ir_move", "to_ir": "ir_move", "stash": "ir_move",
    "reserve": "ir_move",
    "from_ir": "activate", "reinstate": "activate",
    "sit": "bench",
}
_NON_ROSTER_SLOTS = {"IR", "TAXI"}
# Single-slot positions whose starter needs a backup for byes. RB/WR are
# covered by FLEX depth and the bye plan's holes.
BACKUP_POSITIONS = ("QB", "TE", "K", "DEF")
HORIZON_WEEKS = 2
MAX_MOVES = 20
# A suggested pickup must raise that week's best lineup by at least this much.
SUGGEST_MIN_GAIN = 1.0
# Bound each suggestion lookup (they project the whole free-agent pool).
SUGGEST_TIMEOUT_SECONDS = 30.0
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

FAILED_NOTE_CODES = (
    ("too many players", "roster_full"),
    ("claimed by another", "claimed_by_other"),
    ("not enough", "insufficient_budget"),
    ("budget", "insufficient_budget"),
    ("locked", "player_locked"),
)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

@dataclass
class PlanContext:
    """Everything a plan is checked against. Built by `load_context`; tests
    build it directly."""
    league: dict
    roster: dict
    rosters: list[dict]
    info: dict[str, dict]            # player_id -> {name, position, team, status}
    ros: dict[str, dict]             # player_id -> ROS entry (weekly_points, bye_weeks...)
    games: dict[int, dict[str, dict]]  # week -> team -> {kickoff, ...}
    now: datetime
    current_week: int
    week: int | None = None          # first week planned (default current)
    season: int | None = None
    drops: dict[str, datetime] = field(default_factory=dict)  # league's latest drops
    owners: dict[str, str] = field(default_factory=dict)       # owner_id -> display name
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.week = self.week or self.current_week
        self.settings = self.league.get("settings") or {}
        positions = self.league.get("roster_positions") or []
        self.slot_list = starting_slot_list(positions)
        self.max_active = sum(1 for s in positions if str(s).upper() not in _NON_ROSTER_SLOTS)
        self.reserve_slots = int(self.settings.get("reserve_slots") or 0)
        self.roster_id = self.roster.get("roster_id")
        self.held: dict[str, dict] = {}
        for r in self.rosters or []:
            for pid in [*(r.get("players") or []), *(r.get("reserve") or []), *(r.get("taxi") or [])]:
                if pid and str(pid) != "0":
                    self.held[str(pid)] = r
        from .ros import playoff_window
        self.last_week = playoff_window(self.settings)[1]
        start = self.settings.get("playoff_week_start")
        self.last_regular = (int(start) - 1) if isinstance(start, int) and start > 1 else self.last_week

    # -- clock --------------------------------------------------------------
    def kickoff(self, team: str | None, week: int) -> datetime | None:
        if not team:
            return None
        return parse_kickoff(((self.games.get(week) or {}).get(team) or {}).get("kickoff"))

    def rollover(self, week: int) -> datetime:
        """When week `week` ends for roster purposes: the waiver-day run after
        its last game (locks lift, waivers process). An estimate."""
        kicks = [k for g in (self.games.get(week) or {}).values()
                 if (k := parse_kickoff(g.get("kickoff")))]
        if kicks:
            return next_run(self.league, max(kicks), weekly=True)
        known = sorted(w for w, g in self.games.items() if g)
        if known:
            ref = min(known, key=lambda w: abs(w - week))
            if ref != week:
                return self.rollover(ref) + timedelta(days=7 * (week - ref))
        base = next_run(self.league, self.now, weekly=True)
        return base + timedelta(days=7 * (week - self.current_week))

    def week_of(self, t: datetime) -> int:
        for w in range(self.current_week, 19):
            if t < self.rollover(w):
                return w
        return 18

    def locked(self, pid: str, t: datetime) -> datetime | None:
        """His kickoff if his game has started by `t` in that week, else None."""
        team = (self.info.get(pid) or {}).get("team")
        kick = self.kickoff(team, self.week_of(t))
        return kick if kick is not None and kick <= t else None

    def waiver_state(self, pid: str, t: datetime, dropped_at: datetime | None) -> dict:
        team = (self.info.get(pid) or {}).get("team")
        w = self.week_of(t)
        dropped = dropped_at or self.drops.get(pid)
        if not team:
            state = waiver_status(self.league, dropped_at=dropped, now=t)
            if state["on_waivers"] is None:
                state.update(on_waivers=False, instant_add=True, claim_processes_at=None,
                             claim_processes_at_local="instant",
                             reason="no NFL team, so no game lock; not dropped recently")
            return state
        return waiver_status(self.league, kickoff=self.kickoff(team, w),
                             previous_kickoff=self.kickoff(team, w - 1),
                             dropped_at=dropped, now=t)

    # -- players ------------------------------------------------------------
    def name(self, pid: str | None) -> str:
        if not pid:
            return "?"
        return (self.info.get(pid) or {}).get("name") or pid

    def position(self, pid: str) -> str:
        return normalize_position((self.info.get(pid) or {}).get("position")) or "?"

    def points(self, pid: str, week: int) -> float:
        weekly = (self.ros.get(pid) or {}).get("weekly_points") or {}
        value = weekly.get(week, weekly.get(str(week), 0.0))
        return float(value or 0.0)

    def playable(self, pid: str, week: int) -> bool:
        entry = self.ros.get(pid)
        if not entry:
            return True  # unknown: not counted as a hole
        return week not in (entry.get("bye_weeks") or []) and week not in (entry.get("injury_weeks") or [])

    def owner_of(self, pid: str) -> dict | None:
        r = self.held.get(pid)
        if r is None or r.get("roster_id") == self.roster_id:
            return None
        return {"roster_id": r.get("roster_id"),
                "owner": self.owners.get(str(r.get("owner_id"))) or r.get("owner_id")}


def _fmt(t: datetime | None) -> str | None:
    """'Wed 21 Oct 09:00' in the user's zone."""
    return t.astimezone(LOCAL_TZ).strftime("%a %d %b %H:%M") if t else None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# Moves: normalisation, names, times
# ---------------------------------------------------------------------------

_STRING_MOVE = re.compile(
    r"^\s*(?P<action>\w+)\s+(?P<player>.+?)"
    r"(?:\s*,?\s+(?:dropping|drop|for)\s+(?P<drop>.+?))?"
    r"(?:\s+(?:at|in|into)\s+(?P<slot>[A-Za-z_/]+))?\s*$", re.I)


def normalize_move(raw: Any) -> tuple[dict | None, str | None]:
    """``(move, None)`` or ``(None, error)``. Accepts dicts and short strings
    ("add Keaton Mitchell drop Tyreek Hill", "start Mitchell at FLEX")."""
    if isinstance(raw, str):
        m = _STRING_MOVE.match(raw)
        if not m:
            return None, f"could not read move {raw!r}"
        raw = {k: v for k, v in m.groupdict().items() if v}
    if not isinstance(raw, dict):
        return None, f"a move must be an object, got {type(raw).__name__}"
    action = str(raw.get("action") or "").strip().lower().replace("-", "_").replace(" ", "_")
    action = _ACTION_ALIASES.get(action, action)
    if action not in ACTIONS:
        return None, f"unknown action {raw.get('action')!r}; use one of {', '.join(ACTIONS)}"
    player = str(raw.get("player") or raw.get("name") or "").strip()
    if not player:
        return None, f"{action}: 'player' is required"
    drop = raw.get("drop")
    slot = raw.get("slot")
    if action == "start" and not slot and isinstance(raw.get("position"), str):
        slot = raw["position"]
    return {
        "action": action, "player": player,
        "drop": str(drop).strip() if drop else None,
        "when": str(raw["when"]).strip() if raw.get("when") not in (None, "") else None,
        "slot": str(slot).strip().upper() if slot else None,
        "week": int(raw["week"]) if str(raw.get("week") or "").isdigit() else None,
    }, None


def _norm(s: str | None) -> str:
    from .opportunity_tools import norm_name
    return norm_name(s)


def resolve_player(ctx: PlanContext, text: str, db=None, prefer_mine: bool = False) -> tuple[str | None, str | None]:
    """``(player_id, note)`` for a name, Sleeper id or team defense; None if unknown.

    The user's own roster is searched first (a last name alone is enough when it
    is unique there), then the athlete cache.
    """
    from .teams import normalize_team

    raw = text.strip()
    if raw.isdigit():
        return raw, None
    team_def = re.match(r"^([A-Za-z]{2,3})(?:\s*(?:D/?ST|DEF|DST|D|defense))?$", raw, re.I)
    if team_def and (code := normalize_team(team_def.group(1).upper())) and (
            raw.upper() == raw or re.search(r"def|dst|d/st|defense", raw, re.I)):
        return code, None
    key = _norm(raw)
    mine = [str(p) for p in [*(ctx.roster.get("players") or []), *(ctx.roster.get("reserve") or [])]
            if p and str(p) != "0"]
    exact = [p for p in mine if _norm(ctx.info.get(p, {}).get("name")) == key]
    if exact:
        return exact[0], None
    partial = [p for p in mine if key and key in _norm(ctx.info.get(p, {}).get("name")).split()]
    if len(partial) == 1:
        return partial[0], None
    league_hits = [p for p, info in ctx.info.items() if _norm(info.get("name")) == key]
    if league_hits:
        return league_hits[0], None
    if db is not None:
        from .ownership_tools import resolve_names
        resolved, _ = resolve_names(db, [raw])
        if resolved:
            entry = resolved[0]
            note = ("ambiguous name — picked " + f"{entry['name']} ({entry.get('position')}, {entry.get('team')})"
                    if entry.get("ambiguous") else None)
            return entry["player_id"], note
    if prefer_mine and len(partial) > 1:
        return None, f"{raw!r} matches several of your players: " + ", ".join(ctx.name(p) for p in partial)
    return None, f"no player called {raw!r} found"


def parse_when(ctx: PlanContext, when: str | None, related: list[str] | None = None) -> tuple[datetime, str | None]:
    """``(time, error)`` for a move's ``when``.

    Understood: empty/"now"; an ISO timestamp; "waivers"/"next waiver run";
    "after week N"; "week N"/"before week N" (the start of week N); "after
    the game"/"after his game" (his game this week); "<weekday> [HH:MM]"
    (next occurrence, Vienna time).
    """
    if not when or when.strip().lower() in ("now", "today", "asap", "immediately"):
        return ctx.now, None
    text = when.strip().lower()
    try:
        parsed = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed).astimezone(UTC), None
    except ValueError:
        pass
    if "waiver" in text:
        return next_run(ctx.league, ctx.now), None
    m = re.search(r"after\s+(?:the\s+)?week[\s-]*(\d+)", text)
    if m:
        return max(ctx.now, ctx.rollover(int(m.group(1)))), None
    m = re.search(r"week[\s-]*(\d+)", text)
    if m:
        w = int(m.group(1))
        return max(ctx.now, ctx.rollover(w - 1)) if w > ctx.current_week else ctx.now, None
    if "after" in text and "game" in text:
        w = ctx.week_of(ctx.now)
        kicks = [k for pid in related or [] if (k := ctx.kickoff((ctx.info.get(pid) or {}).get("team"), w))]
        if not kicks:
            return ctx.now, "no kickoff known for 'after the game'; treated as now"
        return max(ctx.now, max(kicks) + timedelta(hours=3, minutes=30)), None
    m = re.match(r"^(?:before\s+|by\s+|on\s+)?(mon|tue|wed|thu|fri|sat|sun)\w*\.?\s*(?:(\d{1,2})(?::(\d{2}))?)?", text)
    if m:
        target = _WEEKDAYS.index(m.group(1))
        hour = int(m.group(2)) if m.group(2) else 0
        minute = int(m.group(3)) if m.group(3) else 0
        local_now = ctx.now.astimezone(LOCAL_TZ)
        day = local_now.date()
        for _ in range(8):
            cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)
            if day.weekday() == target and cand > local_now:
                t = cand.astimezone(UTC)
                # "before Wed 09:00" means just ahead of it.
                return (t - timedelta(minutes=1) if text.startswith(("before", "by")) else t), None
            day += timedelta(days=1)
    return ctx.now, f"could not read when={when!r}; treated as now"


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class _State:
    active: list[str]
    reserve: list[str]
    dropped: dict[str, datetime] = field(default_factory=dict)
    added: set[str] = field(default_factory=set)

    def copy(self) -> _State:
        return _State(list(self.active), list(self.reserve), dict(self.dropped), set(self.added))

    def has(self, pid: str) -> bool:
        return pid in self.active or pid in self.reserve


def _initial_state(ctx: PlanContext) -> _State:
    reserve = [str(p) for p in ctx.roster.get("reserve") or [] if p and str(p) != "0"]
    taxi = {str(p) for p in ctx.roster.get("taxi") or []}
    active = [str(p) for p in ctx.roster.get("players") or []
              if p and str(p) != "0" and str(p) not in reserve and str(p) not in taxi]
    return _State(active, reserve)


def _blocking_ir(ctx: PlanContext, state: _State) -> list[str]:
    """IR players Sleeper no longer accepts there: they block adds and claims."""
    return [p for p in state.reserve
            if not is_ir_eligible((ctx.info.get(p) or {}).get("status"), ctx.settings)]


def _counts(ctx: PlanContext, state: _State) -> str:
    return (f"{len(state.active)}/{ctx.max_active} active"
            + (f", IR {len(state.reserve)}/{ctx.reserve_slots}" if ctx.reserve_slots else ""))


def _check_drop(ctx: PlanContext, state: _State, pid: str, t: datetime) -> str | None:
    if not state.has(pid):
        return f"{ctx.name(pid)} is not on your roster at that point"
    kick = ctx.locked(pid, t)
    if kick is not None and pid not in state.reserve:
        return (f"{ctx.name(pid)}'s game kicked off {_fmt(kick)} — Sleeper locks him until the "
                f"week rolls over (~{_fmt(ctx.rollover(ctx.week_of(t)))}), so he cannot be dropped")
    return None


def _apply_drop(state: _State, pid: str, t: datetime) -> None:
    if pid in state.active:
        state.active.remove(pid)
    if pid in state.reserve:
        state.reserve.remove(pid)
    state.dropped[pid] = t


def simulate(ctx: PlanContext, moves: list[dict], db=None) -> dict:
    """Replay `moves` (normalised, see `normalize_move`) in time order.

    Returns ``{steps, roster, weeks, warnings, lost_backups, checklist,
    verify_after, valid, invalid_steps}``. Each step carries the move as
    resolved, when it takes effect (a claim: at the waiver run), the week it
    first affects, ``valid`` and — when invalid — ``reason`` and ``fix``.
    """
    base = _initial_state(ctx)
    state = base.copy()
    resolved: list[dict] = []
    for i, move in enumerate(moves, 1):
        step: dict[str, Any] = {"step": i, "action": move["action"], "requested_when": move.get("when"),
                                "valid": True, "notes": []}
        pid, note = resolve_player(ctx, move["player"], db, prefer_mine=move["action"] != "add")
        drop_pid = None
        if note and pid:
            step["notes"].append(note)
        if pid is None:
            step.update(valid=False, reason=note or "player not found")
        if move.get("drop"):
            drop_pid, dnote = resolve_player(ctx, move["drop"], db, prefer_mine=True)
            if drop_pid is None:
                step.update(valid=False, reason=dnote or "drop player not found")
        step["player"] = _who(ctx, pid) if pid else {"name": move["player"]}
        step["drop"] = _who(ctx, drop_pid) if drop_pid else (
            {"name": move["drop"]} if move.get("drop") else None)
        when, werr = parse_when(ctx, move.get("when"), [p for p in (pid, drop_pid) if p])
        if move.get("week") and not move.get("when") and move["week"] > ctx.current_week:
            when = max(ctx.now, ctx.rollover(move["week"] - 1))
        if werr:
            step["notes"].append(werr)
        resolved.append({"step": step, "move": move, "pid": pid, "drop_pid": drop_pid, "t": when})

    # A claim takes effect when it processes; everything else when asked.
    for r in resolved:
        r["effective"] = r["t"]
        if r["move"]["action"] == "claim" and r["pid"]:
            ws = ctx.waiver_state(r["pid"], r["t"], None)
            if ws.get("claim_processes_at"):
                r["effective"] = max(r["t"], datetime.fromisoformat(ws["claim_processes_at"]))
            elif ws.get("on_waivers") is None:
                r["effective"] = next_run(ctx.league, r["t"])
    order = sorted(resolved, key=lambda r: r["effective"])

    forced: dict[int, dict[str, str | None]] = {}
    benched: dict[int, set[str]] = {}
    snapshots: list[tuple[int, _State]] = []
    checklist: list[dict] = []
    verify: list[str] = []

    failed_adds: dict[str, int] = {}
    for r in order:
        step, move, pid, drop_pid = r["step"], r["move"], r["pid"], r["drop_pid"]
        t: datetime = r["effective"]
        week = ctx.week_of(t)
        if move["action"] in ("start", "bench") and move.get("week"):
            week = max(week, move["week"])
        step.update(effective_at=t.isoformat(), effective_at_local=local_time(t), week=week)
        if step["valid"]:
            _run_step(ctx, state, r, t, week, step, forced, benched, checklist, verify)
        if not step["valid"] and pid:
            if move["action"] in ("add", "claim"):
                failed_adds.setdefault(pid, step["step"])
            elif pid in failed_adds and not state.has(pid):
                step["reason"] = (f"{step.get('reason')} It depends on step {failed_adds[pid]}, "
                                  "which adds him and is itself invalid.")
                step["depends_on"] = failed_adds[pid]
        step["roster_after"] = _counts(ctx, state)
        snapshots.append((week, state.copy()))

    steps = [r["step"] for r in resolved]
    for r in order:
        r["step"]["order"] = order.index(r) + 1

    # Weeks to report: the next HORIZON_WEEKS from the plan week, plus every
    # week a step touches.
    plan_week = cast(int, ctx.week)
    touched = {s["week"] for s in steps if s.get("week")}
    weeks = sorted(set(range(plan_week, plan_week + HORIZON_WEEKS)) | {w for w in touched if w >= plan_week})
    weeks = [w for w in weeks if w <= ctx.last_week]
    week_rows = []
    warnings: list[str] = []
    def state_at(w: int) -> _State:
        st = base
        for sw, snap in snapshots:
            if sw <= w:
                st = snap
        return st

    for w in weeks:
        st = state_at(w)
        before = _lineup(ctx, base.active, w, {}, set())
        after = _lineup(ctx, st.active, w, forced.get(w, {}), benched.get(w, set()))
        row = {"week": w, "roster": [_who(ctx, p) for p in st.active],
               "reserve": [_who(ctx, p) for p in st.reserve],
               "lineup": after["lineup"], "projected_total": after["total"],
               "projected_total_before": before["total"],
               "change": round(after["total"] - before["total"], 1),
               "empty_slots": after["holes"], "notes": after["notes"]}
        bye = _bye_row(ctx, st.active, w)
        if bye:
            row.update(status=bye["status"], bye_cost=bye["bye_cost"],
                       starters_on_bye=[b["player"] for b in bye["on_bye"] if b["role"] == "starter"])
            if bye["status"] == "crunch" and w < plan_week + HORIZON_WEEKS:
                warnings.append(f"Week {w}: bye crunch — lineup {bye['projected_total']} vs "
                                f"{bye['full_strength_total']} at full strength"
                                + (f", on bye: {', '.join(b['player'] for b in bye['on_bye'])}"
                                   if bye["on_bye"] else "") + ".")
        if after["holes"] and w < plan_week + HORIZON_WEEKS:
            warnings.append(f"Week {w}: {len(after['holes'])} empty starting slot(s) after the plan — "
                            + ", ".join(h["slot"] for h in after["holes"]) + ".")
        week_rows.append(row)

    lost = _lost_backups(ctx, base, state, plan_week, state_at)
    for entry in lost:
        warnings.append(entry["message"])
    blocking = _blocking_ir(ctx, state)
    if blocking:
        warnings.append("Still in IR but no longer eligible: " + ", ".join(ctx.name(p) for p in blocking)
                        + " — Sleeper refuses every add and claim until he is activated.")

    # Execution order (what has to happen first), each with its own deadline.
    checklist.sort(key=lambda c: c["_sort"])
    for c in checklist:
        c.pop("_sort", None)
    invalid = [s["step"] for s in steps if not s["valid"]]
    verify.append("Nothing in this plan has been entered in Sleeper — the tool only simulates. "
                  "Treat a move as done only once plan_roster_moves(verify=True) or get_transactions shows it.")
    return {
        "valid": not invalid,
        "invalid_steps": invalid,
        "steps": steps,
        "roster": {"active": [_who(ctx, p) for p in state.active],
                   "reserve": [_who(ctx, p) for p in state.reserve],
                   "counts": _counts(ctx, state), "max_active": ctx.max_active,
                   "reserve_slots": ctx.reserve_slots},
        "weeks": week_rows,
        "warnings": warnings,
        "lost_backups": lost,
        "checklist": checklist,
        "verify_after": list(dict.fromkeys(verify)),
    }


def _who(ctx: PlanContext, pid: str | None) -> dict:
    info = ctx.info.get(pid or "") or {}
    return {"name": ctx.name(pid), "player_id": pid, "position": ctx.position(pid) if pid else None,
            "team": info.get("team"), "sleeper_status": info.get("status")}


def _invalid(step: dict, reason: str, fix: str | None = None) -> None:
    step["valid"] = False
    step["reason"] = reason
    if fix:
        step["fix"] = fix


def _drop_hint(ctx: PlanContext, state: _State, t: datetime, keep: set[str]) -> str:
    cand = _drop_for(ctx, state, keep, t, ctx.week_of(t))
    return (f" Least valuable droppable bench player at that point: {ctx.name(cand)} "
            f"({ctx.position(cand)})." if cand else "")


def _space_fix(ctx: PlanContext, state: _State, pid: str | None, t: datetime) -> str:
    ir_ready = [p for p in state.active
                if is_ir_eligible((ctx.info.get(p) or {}).get("status"), ctx.settings)]
    hint = _drop_hint(ctx, state, t, {pid} if pid else set())
    if ir_ready and len(state.reserve) < ctx.reserve_slots:
        return (f"Add a drop, or first move {ctx.name(ir_ready[0])} to IR "
                f"({(ctx.info.get(ir_ready[0]) or {}).get('status')} is IR-eligible here).{hint}")
    return f"Pair it with a drop (\"drop\": <player>) — the roster is full at that point.{hint}"


def _run_step(ctx: PlanContext, state: _State, r: dict, t: datetime, week: int, step: dict,
              forced: dict[int, dict[str, str | None]], benched: dict[int, set[str]],
              checklist: list[dict], verify: list[str]) -> None:
    action = r["move"]["action"]
    pid: str = r["pid"]
    drop_pid: str | None = r["drop_pid"]
    name = ctx.name(pid)
    settings = ctx.settings

    def item(deadline: datetime | None, text: str, start: datetime | None = None) -> None:
        checklist.append({
            "step": step["step"], "do": text,
            "not_before": local_time(start) if start and start > ctx.now + timedelta(minutes=1) else None,
            "deadline": local_time(deadline),
            "_sort": (max(start or ctx.now, ctx.now), len(checklist)),
        })

    if action in ("add", "claim"):
        if state.has(pid):
            return _invalid(step, f"{name} is already on your roster.")
        owner = ctx.owner_of(pid)
        if owner and pid not in state.dropped:
            return _invalid(step, f"{name} is rostered by roster {owner['roster_id']}"
                                  + (f" ({owner['owner']})" if owner.get("owner") else "") + ".",
                            "Only a trade gets him (find_trade_targets / analyze_trade).")
        blocking = _blocking_ir(ctx, state)
        if blocking:
            return _invalid(step, "Sleeper refuses every add and claim while "
                                  + ", ".join(f"{ctx.name(p)} ({(ctx.info.get(p) or {}).get('status') or 'healthy'})"
                                              for p in blocking)
                                  + " sits in IR without an eligible status.",
                            f"Activate {ctx.name(blocking[0])} first (with a drop if the roster is full).")
        ws = ctx.waiver_state(pid, r["t"], state.dropped.get(pid))
        step["waiver_timing"] = {k: ws.get(k) for k in ("on_waivers", "reason", "clears_at_local",
                                                         "claim_processes_at_local", "in_time_for_kickoff")}
        if action == "add" and ws.get("on_waivers"):
            return _invalid(step, f"{name} is on waivers ({ws['reason']}) — not an instant add.",
                            f"Enter a claim instead (processes ~{_fmt(_dt(ws.get('claim_processes_at')))}), "
                            "or add him after he clears"
                            + (f" ({_fmt(_dt(ws.get('clears_at')))})." if ws.get("clears_at") else "."))
        if action == "claim" and ws.get("on_waivers") is False:
            step["notes"].append(f"{name} is a free agent at that time — no claim needed: add him "
                                 "instantly (a claim would wait for the waiver run).")
            action = "add"
        if ws.get("on_waivers") is None:
            step["notes"].append("Whether he is on waivers is unknown (no cached kickoffs) — check the app.")
        if action == "add":
            kick = ctx.locked(pid, t)
            if kick is not None:
                return _invalid(step, f"{name}'s game kicked off {_fmt(kick)}: he is locked on waivers.",
                                "Claim him for the next waiver-day run instead.")
        drop_err = None
        if drop_pid:
            drop_err = _check_drop(ctx, state, drop_pid, t)
            if drop_err:
                return _invalid(step, drop_err, "Pick a drop who is on your roster and whose game has "
                                                "not started (or wait for the week to roll over)."
                                + _drop_hint(ctx, state, t, {pid}))
        if len(state.active) - (1 if drop_pid and drop_pid in state.active else 0) + 1 > ctx.max_active:
            when_txt = ("when the claim processes" if action == "claim" else "after this add")
            return _invalid(step, f"Your roster will have too many players {when_txt}: "
                                  f"{_counts(ctx, state)} with no drop — Sleeper rejects it "
                                  "(\"Unfortunately, your roster will have too many players after "
                                  "this transaction\").", _space_fix(ctx, state, pid, t))
        if drop_pid:
            _apply_drop(state, drop_pid, t)
        state.active.append(pid)
        state.added.add(pid)
        drop_txt = f", dropping {ctx.name(drop_pid)}" if drop_pid else ""
        if action == "add":
            w = ctx.week_of(t)
            locks = [k for p in (pid, drop_pid) if p
                     and (k := ctx.kickoff((ctx.info.get(p) or {}).get("team"), w)) and k > t]
            deadline = min(locks) if locks else None
            item(deadline, f"Add {name} (free agent, instant){drop_txt}"
                 + (f" — before {_fmt(deadline)}, when the first of them kicks off" if deadline else ""), r["t"])
            verify.append(f"Right after adding {name}: confirm he is on your roster "
                          "(plan_roster_moves verify=True). Do not assume the add went through.")
            step["kind"] = "instant_add"
        else:
            run = t
            rules = waiver_rules(ctx.league)
            pos = (ctx.roster.get("settings") or {}).get("waiver_position")
            teams = ctx.league.get("total_rosters")
            prio = ""
            if rules["waiver_type"] == "faab":
                prio = " — FAAB: set a bid (recommend_faab_bid)"
            elif isinstance(pos, int) and pos > 0:
                prio = (f" — you are {_ordinal(pos)} of {teams} in priority"
                        + ("; rolling waivers: a successful claim sends you to the back"
                           if settings.get("waiver_type") == 0 else ""))
                if pos > 1:
                    step["notes"].append(f"{pos - 1} team(s) ahead of you can claim {name} first.")
            item(run - timedelta(minutes=1),
                 f"Enter the claim for {name}{drop_txt} before the waiver run (~{_fmt(run)}){prio}.",
                 r["t"])
            verify.append(f"After ~{_fmt(run)}: check whether the claim for {name} processed "
                          "(plan_roster_moves verify=True or get_transactions) — claims are invisible "
                          "until processed; a failed one carries Sleeper's reason.")
            step["kind"] = "waiver_claim"
            if ws.get("in_time_for_kickoff") is False:
                step["notes"].append(f"The claim lands after {name}'s kickoff this week — he only "
                                     "helps from next week.")
        return None

    if action == "drop":
        err = _check_drop(ctx, state, pid, t)
        if err:
            return _invalid(step, err, "Drop him once the week rolls over, or drop someone whose game "
                                       "has not started.")
        _apply_drop(state, pid, t)
        clear = settings.get("waiver_clear_days")
        step["notes"].append(f"{name} goes on waivers for {clear if clear is not None else 2} day(s) "
                             "— any team can claim him.")
        kick = ctx.kickoff((ctx.info.get(pid) or {}).get("team"), ctx.week_of(t))
        item(kick if kick and kick > t else None,
             f"Drop {name}" + (f" — before his game locks him ({_fmt(kick)})" if kick and kick > t else ""),
             r["t"])
        return None

    if action == "ir_move":
        status = (ctx.info.get(pid) or {}).get("status")
        if pid in state.reserve:
            return _invalid(step, f"{name} is already in IR.")
        if pid not in state.active:
            return _invalid(step, f"{name} is not on your active roster at that point.")
        if not ctx.reserve_slots:
            return _invalid(step, "This league has no IR slot.")
        if not is_ir_eligible(status, settings):
            return _invalid(step, f"{name} is listed {status or 'healthy'} by Sleeper; this league only "
                                  f"allows {', '.join(eligible_statuses(settings))} in IR.",
                            "Keep him on the bench or drop him; recheck after the next Sleeper status update.")
        if len(state.reserve) >= ctx.reserve_slots:
            return _invalid(step, f"All {ctx.reserve_slots} IR slot(s) are taken ("
                                  + ", ".join(ctx.name(p) for p in state.reserve) + ").",
                            "Activate (or drop) the player in IR first.")
        state.active.remove(pid)
        state.reserve.append(pid)
        item(None, f"Move {name} to IR ({status}) — frees a bench spot", r["t"])
        return None

    if action == "activate":
        if pid not in state.reserve:
            return _invalid(step, f"{name} is not in your IR slot at that point.")
        if drop_pid:
            err = _check_drop(ctx, state, drop_pid, t)
            if err:
                return _invalid(step, err)
        if len(state.active) - (1 if drop_pid and drop_pid in state.active else 0) + 1 > ctx.max_active:
            return _invalid(step, f"Activating {name} needs a free roster spot: {_counts(ctx, state)}.",
                            "Drop someone first (\"drop\": <player>, or a drop step before it).")
        if drop_pid:
            _apply_drop(state, drop_pid, t)
        state.reserve.remove(pid)
        state.active.append(pid)
        if not ctx.playable(pid, week):
            step["notes"].append(f"{name} is still projected out in week {week}.")
        item(None, f"Activate {name} from IR"
             + (f", dropping {ctx.name(drop_pid)}" if drop_pid else ""), r["t"])
        return None

    # start / bench
    if pid not in state.active:
        return _invalid(step, f"{name} is not on your active roster in week {week}"
                              + (" (he is in IR)" if pid in state.reserve else "") + ".")
    kick = ctx.locked(pid, t)
    if kick is not None and week == ctx.week_of(t):
        return _invalid(step, f"{name}'s game kicked off {_fmt(kick)} — his lineup spot is locked.")
    team = (ctx.info.get(pid) or {}).get("team")
    deadline = ctx.kickoff(team, week)
    if action == "bench":
        benched.setdefault(week, set()).add(pid)
        forced.get(week, {}).pop(pid, None)
        item(deadline, f"Week {week}: bench {name}", r["t"])
        return None
    slot = r["move"].get("slot")
    pos = ctx.position(pid)
    if slot and not slot_accepts(slot, pos):
        return _invalid(step, f"A {pos} cannot start at {slot} in this league.")
    if slot and not any(normalize_slot(s) == normalize_slot(slot) for s in ctx.slot_list):
        return _invalid(step, f"This league has no {slot} slot ({', '.join(ctx.slot_list)}).")
    if week == ctx.current_week:
        raw = [str(p) for p in ctx.roster.get("starters") or []]
        held = {i for i, p in enumerate(raw[:len(ctx.slot_list)])
                if p in state.active and ctx.locked(p, t) is not None}
        open_slots = [i for i, s in enumerate(ctx.slot_list) if i not in held and slot_accepts(s, pos)
                      and (not slot or normalize_slot(s) == normalize_slot(slot))]
        if not open_slots:
            lockers = ", ".join(ctx.name(raw[i]) for i in sorted(held)
                                if not slot or normalize_slot(ctx.slot_list[i]) == normalize_slot(slot))
            return _invalid(step, f"Every {slot or pos}-eligible slot is held by a player whose game "
                                  f"has started ({lockers}) — the lineup cannot change there.")
    if not ctx.playable(pid, week):
        entry = ctx.ros.get(pid) or {}
        why = "on bye" if week in (entry.get("bye_weeks") or []) else "projected out (injury)"
        return _invalid(step, f"{name} is {why} in week {week} — starting him scores nothing.")
    forced.setdefault(week, {})[pid] = slot
    (benched.get(week) or set()).discard(pid)
    item(deadline, f"Week {week}: start {name}" + (f" at {slot}" if slot else ""), r["t"])
    return None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _lineup(ctx: PlanContext, active: list[str], week: int,
            forced: dict[str, str | None], benched: set[str]) -> dict:
    """Best lineup for `week` from `active`, with forced starts and benchings.

    In the current week a locked starter keeps his slot and a locked bench
    player stays benched — Sleeper no longer lets either move.
    """
    slots = list(ctx.slot_list)
    fixed: dict[int, str] = {}
    excluded = set(benched)
    notes: list[str] = []
    if week == ctx.current_week:
        raw = [str(p) for p in ctx.roster.get("starters") or []]
        for i, (_slot, pid) in enumerate(zip(slots, raw, strict=False)):
            if pid in active and ctx.locked(pid, ctx.now):
                fixed[i] = pid
        excluded |= {p for p in active if p not in fixed.values() and ctx.locked(p, ctx.now)}
    for pid, slot in forced.items():
        if pid in fixed.values() or pid not in active:
            continue
        pos = ctx.position(pid)
        free = [i for i in range(len(slots)) if i not in fixed and slot_accepts(slots[i], pos)]
        if slot:
            free = [i for i in free if normalize_slot(slots[i]) == normalize_slot(slot)]
        else:
            free.sort(key=lambda i: len(SLOT_ELIGIBILITY.get(normalize_slot(slots[i]), ())))
        if free:
            fixed[free[0]] = pid
        else:
            notes.append(f"No free {slot or pos} slot left for {ctx.name(pid)}.")
    pool = [{"player_id": p, "position": ctx.position(p)} for p in active
            if p not in fixed.values() and p not in excluded and ctx.points(p, week) > 0]
    rest = [i for i in range(len(slots)) if i not in fixed]
    picks = optimal_lineup(pool, [slots[i] for i in rest], value=lambda p: ctx.points(p["player_id"], week))
    seat: dict[int, str | None] = {i: fixed[i] for i in fixed}
    for i, p in zip(rest, picks, strict=True):
        seat[i] = p["player_id"] if p else None
    lineup: list[dict] = []
    holes: list[dict] = []
    total = 0.0
    for i, slot in enumerate(slots):
        seated = seat.get(i)
        pts = ctx.points(seated, week) if seated else 0.0
        total += pts
        row: dict[str, Any] = {"slot": slot, "player": ctx.name(seated) if seated else None,
                               "position": ctx.position(seated) if seated else None,
                               "projected_points": round(pts, 1)}
        if seated and i in fixed:
            row["fixed"] = ("locked" if week == ctx.current_week and ctx.locked(seated, ctx.now)
                            else "forced")
        lineup.append(row)
        if seated is None:
            holes.append({"slot": slot, "eligible": sorted(SLOT_ELIGIBILITY.get(normalize_slot(slot), {slot}))})
    return {"lineup": lineup, "holes": holes, "total": round(total, 1), "notes": notes}


def _bye_row(ctx: PlanContext, active: list[str], week: int) -> dict | None:
    """The bye plan's own week row (crunch / thin / ok) for this roster."""
    from .bye_week_tools import _rate, _week_row
    entries = [ctx.ros[p] for p in active if p in ctx.ros]
    if not entries:
        return None
    core = optimal_lineup(entries, ctx.slot_list, value=_rate)
    try:
        return _week_row(entries, ctx.slot_list, week, {p["player_id"] for p in core if p})
    except Exception as e:  # an entry missing a field: report without the bye row
        logger.debug(f"bye row for week {week} failed: {e}")
        return None


def _backup_positions(ctx: PlanContext) -> dict[str, int]:
    need: dict[str, int] = {}
    for slot in ctx.slot_list:
        eligible = SLOT_ELIGIBILITY.get(normalize_slot(slot), frozenset())
        if len(eligible) == 1:
            pos = next(iter(eligible))
            if pos in BACKUP_POSITIONS:
                need[pos] = need.get(pos, 0) + 1
    return need


def _lost_backups(ctx: PlanContext, base: _State, final: _State, plan_week: int,
                  state_at: Callable[[int], _State] | None = None) -> list[dict]:
    """Single-slot positions the plan leaves without enough bodies for a bye.

    A week counts as uncovered only once the plan's roster for that week is
    short where the current roster was not — a bye that has already passed
    when the backup is dropped costs nothing.
    """
    out = []
    weeks = list(range(plan_week, ctx.last_regular + 1))
    state_at = state_at or (lambda _w: final)
    for pos, need in _backup_positions(ctx).items():
        before = [p for p in base.active if ctx.position(p) == pos]
        after = [p for p in final.active if ctx.position(p) == pos]
        if len(after) >= len(before) or len(after) > need:
            continue
        uncovered = [w for w in weeks
                     if _playing(ctx, state_at(w), pos, w) < need <= _playing(ctx, base, pos, w)]
        gone = [ctx.name(p) for p in before if p not in after]
        left = ", ".join(ctx.name(p) for p in after) or "nobody"
        reasons = []
        for w in uncovered:
            out_now = [ctx.name(p) for p in after if not ctx.playable(p, w)]
            reasons.append(f"week {w}" + (f" ({', '.join(out_now)} on bye/out)" if out_now else ""))
        message = (f"Lost backup at {pos}: dropping {', '.join(gone)} leaves {left} for "
                   f"{need} {pos} slot(s)"
                   + (f" — no {pos} in " + "; ".join(reasons) + "." if uncovered
                      else " — covered every remaining week, but an injury leaves the slot empty."))
        out.append({"position": pos, "slots": need, "before": len(before), "after": len(after),
                    "dropped": gone, "remaining": [ctx.name(p) for p in after],
                    "uncovered_weeks": uncovered, "message": message})
    return out


# ---------------------------------------------------------------------------
# Failed claims and planned-vs-happened
# ---------------------------------------------------------------------------

def failure_code(note: str | None) -> str:
    text = (note or "").lower()
    for needle, code in FAILED_NOTE_CODES:
        if needle in text:
            return code
    return "other" if text else "unknown"


def failed_claims(transactions: list[dict] | None, names: dict[str, str] | None = None) -> list[dict]:
    """Failed waiver/free-agent transactions with Sleeper's own note
    (``metadata.notes``) and a ``reason_code`` — roster_full,
    claimed_by_other, insufficient_budget, player_locked, other."""
    names = names or {}
    out = []
    for tx in transactions or []:
        if not isinstance(tx, dict) or (tx.get("status") or "").lower() != "failed":
            continue
        if tx.get("type") not in ("waiver", "free_agent"):
            continue
        note = (tx.get("metadata") or {}).get("notes")
        stamp = tx.get("status_updated") or tx.get("created")

        enriched = {str(e.get("player_id")): e.get("full_name")
                    for e in (tx.get("adds_enriched") or []) + (tx.get("drops_enriched") or [])
                    if isinstance(e, dict)}

        def _named(ids: dict | None, enriched: dict = enriched) -> list[dict]:
            return [{"player_id": str(p), "name": enriched.get(str(p)) or names.get(str(p)) or str(p)}
                    for p in (ids or {})]
        out.append({
            "transaction_id": tx.get("transaction_id"),
            "roster_ids": tx.get("roster_ids") or [],
            "type": tx.get("type"),
            "wanted": _named(tx.get("adds")),
            "would_have_dropped": _named(tx.get("drops")),
            "note": note,
            "reason_code": failure_code(note),
            "processed_at": (local_time(datetime.fromtimestamp(stamp / 1000, UTC))
                             if isinstance(stamp, (int, float)) else None),
            "leg": tx.get("leg"),
        })
    return out


def _stamp(tx: dict) -> datetime | None:
    value = tx.get("status_updated") or tx.get("created")
    return datetime.fromtimestamp(value / 1000, UTC) if isinstance(value, (int, float)) else None


def diff_planned_vs_happened(ctx: PlanContext, moves: list[dict], roster_now: dict,
                             transactions: list[dict], db=None, since: datetime | None = None) -> dict:
    """For each planned move: done, failed (with Sleeper's note), taken by
    another team, pending (claim not yet processed) or not happened.

    Only transactions processed at or after `since` count (default: two days
    before the waiver run that opened the current week), so an old add or
    an old failed claim for the same player is not read as this plan's.
    """
    rid = ctx.roster_id
    players_now = {str(p) for p in roster_now.get("players") or []}
    reserve_now = {str(p) for p in roster_now.get("reserve") or []}
    starters_now = {str(p) for p in roster_now.get("starters") or []}
    if since is None:
        since = (ctx.rollover(ctx.current_week - 1) if ctx.current_week > 1 else ctx.now) - timedelta(days=2)
    txs = sorted([t for t in transactions or [] if isinstance(t, dict)
                  and ((st := _stamp(t)) is None or st >= since)],
                 key=lambda t: t.get("status_updated") or t.get("created") or 0)

    def mine(tx: dict) -> bool:
        return rid in (tx.get("roster_ids") or []) or str(rid) in [str(x) for x in tx.get("roster_ids") or []]

    def last_tx(pid: str, key: str, only_mine: bool = True) -> dict | None:
        hits = [t for t in txs if pid in {str(p) for p in (t.get(key) or {})} and (mine(t) or not only_mine)]
        return hits[-1] if hits else None

    rows: list[dict] = []
    for i, move in enumerate(moves, 1):
        pid, _ = resolve_player(ctx, move["player"], db)
        drop_pid = resolve_player(ctx, move["drop"], db)[0] if move.get("drop") else None
        row: dict[str, Any] = {"step": i, "action": move["action"], "player": ctx.name(pid) if pid else move["player"],
                               "drop": ctx.name(drop_pid) if drop_pid else move.get("drop")}
        if pid is None:
            row.update(status="unknown_player", detail="could not resolve the player name")
            rows.append(row)
            continue
        action = move["action"]
        if action in ("add", "claim"):
            tx = last_tx(pid, "adds")
            if tx and _stamp(tx):
                row["processed_at"] = local_time(_stamp(tx))
            if pid in players_now or pid in reserve_now:
                row.update(status="done", detail=f"{ctx.name(pid)} is on your roster")
                if drop_pid and (drop_pid in players_now or drop_pid in reserve_now):
                    row.update(status="partial", detail=f"{ctx.name(pid)} added but {ctx.name(drop_pid)} "
                                                        "is still on your roster")
            elif tx and (tx.get("status") or "").lower() == "failed":
                note = (tx.get("metadata") or {}).get("notes")
                row.update(status="failed", reason_code=failure_code(note), note=note,
                           detail=f"Sleeper rejected it: {note or 'no reason given'}")
            elif (owner := ctx.owner_of(pid)) is not None and owner["roster_id"] != rid:
                other = last_tx(pid, "adds", only_mine=False)
                row.update(status="taken_by_other", roster_id=owner["roster_id"], owner=owner.get("owner"),
                           detail=f"{ctx.name(pid)} is now on roster {owner['roster_id']}"
                                  + (f" ({owner['owner']})" if owner.get("owner") else "")
                                  + (f" via a {other.get('type')}" if other else ""))
            elif tx and (tx.get("status") or "complete") == "complete":
                row.update(status="done_then_dropped", detail="the add went through but he has left the roster since")
            else:
                ws = ctx.waiver_state(pid, ctx.now, None)
                run = next_run(ctx.league, ctx.now - timedelta(hours=1))
                if action == "claim" and ws.get("on_waivers"):
                    row.update(status="pending_or_not_entered",
                               detail=f"No processed claim yet. He is on waivers until ~{_fmt(_dt(ws.get('clears_at')))}: "
                                      "a pending claim is invisible to the API — confirm it is in the app's "
                                      "pending claims, otherwise it was never entered.")
                else:
                    row.update(status="not_happened",
                               detail=f"No transaction and he is not on your roster — the "
                                      f"{'claim' if action == 'claim' else 'add'} was never processed. "
                                      + ("He is a free agent: add him now." if ws.get("on_waivers") is False
                                         else f"Next waiver run ~{_fmt(run)}."))
        elif action == "drop":
            gone = pid not in players_now and pid not in reserve_now
            row.update(status="done" if gone else "not_happened",
                       detail=f"{ctx.name(pid)} {'is off' if gone else 'is still on'} your roster")
        elif action == "ir_move":
            row.update(status="done" if pid in reserve_now else "not_happened",
                       detail=f"{ctx.name(pid)} {'is' if pid in reserve_now else 'is not'} in IR")
        elif action == "activate":
            ok = pid in players_now and pid not in reserve_now
            row.update(status="done" if ok else "not_happened",
                       detail=f"{ctx.name(pid)} {'is' if ok else 'is not'} on the active roster")
        else:
            starting = pid in starters_now
            want = action == "start"
            row.update(status="done" if starting == want else "not_happened",
                       detail=f"{ctx.name(pid)} is {'in' if starting else 'out of'} the current lineup"
                              + (" (only the current week's lineup can be checked)" if move.get("week") else ""))
        rows.append(row)
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"moves": rows, "summary": summary, "since": local_time(since),
            "failed_claims": [f for f in failed_claims(txs, {p: ctx.name(p) for p in ctx.info})
                              if rid in f["roster_ids"] or str(rid) in [str(x) for x in f["roster_ids"]]]}


# ---------------------------------------------------------------------------
# Loading and the tool
# ---------------------------------------------------------------------------

async def load_context(league_id: str, roster_id: int, week: int | None, names: list[str],
                       db=None, purpose: str = "availability") -> tuple[PlanContext | None, str | None, dict]:
    """``(context, error, roster_state)`` from live Sleeper data and the DB."""
    from . import ros, sleeper_tools
    from .database import get_shared_db
    from .game_clock import week_games
    from .ownership_tools import _owners, _timing_context
    from .teams import normalize_team
    from .week_context import current_season_week

    db = db if db is not None else get_shared_db()
    current = await current_season_week(db)
    season, cweek = int(current["season"]), int(current["week"])
    league_resp, roster_state, users_resp = await asyncio.gather(
        sleeper_tools.get_league(league_id), sleeper_tools.load_rosters(league_id, purpose),
        sleeper_tools.get_league_users(league_id), return_exceptions=True)
    if isinstance(roster_state, BaseException):
        return None, f"Could not load rosters: {roster_state}", {}
    league = {} if isinstance(league_resp, BaseException) else (league_resp or {}).get("league") or {}
    if not league:
        return None, f"Could not load league {league_id}.", roster_state
    if roster_state.get("blocking_error"):
        return None, roster_state["blocking_error"], roster_state
    rosters = roster_state["rosters"]
    mine, error = sleeper_tools.find_roster(rosters, league_id, roster_id, None, purpose="plan")
    if error or mine is None:
        return None, error or "roster not found", roster_state
    users = [] if isinstance(users_resp, BaseException) else (users_resp or {}).get("users") or []
    owners = {k: str(v.get("display_name") or k) for k, v in _owners(users).items()}

    plan_week = max(int(week or cweek), cweek)
    weeks_needed = range(max(1, cweek - 1), min(18, max(plan_week, cweek) + HORIZON_WEEKS + 2) + 1)
    games = {w: week_games(db, season, w) for w in weeks_needed}
    for w in range(plan_week, 19):  # rest of season for bye / backup checks
        games.setdefault(w, week_games(db, season, w))

    my_ids = [str(p) for p in [*(mine.get("players") or []), *(mine.get("reserve") or [])] if p and str(p) != "0"]
    rows = db.get_athletes_by_ids(my_ids)
    info: dict[str, dict] = {}

    def _info(pid: str, row: dict) -> dict:
        from .injury_match import sleeper_injury_status
        pos = normalize_position(row.get("position"))
        team = normalize_team(row.get("team_id") or row.get("team")) or None
        return {"name": row.get("full_name") or (team if pos == "DEF" else pid),
                "position": pos, "team": team, "status": sleeper_injury_status(row)}

    for pid in my_ids:
        info[pid] = _info(pid, rows.get(pid) or {"position": "DEF" if not pid.isdigit() else None,
                                                  "team_id": pid if not pid.isdigit() else None})
    ctx = PlanContext(league=league, roster=mine, rosters=rosters, info=info, ros={}, games=games,
                      now=datetime.now(UTC), current_week=cweek, week=plan_week, season=season,
                      owners=owners)
    # Named players outside the roster.
    extra = []
    for name in names:
        found, _ = resolve_player(ctx, name, db)
        if found and found not in info:
            extra.append(found)
    if extra:
        more = db.get_athletes_by_ids(extra)
        for pid in extra:
            row = more.get(pid) or ({"position": "DEF", "team_id": pid} if not pid.isdigit() else {})
            info[pid] = _info(pid, row)
    timing = await _timing_context(db, league_id, season, cweek)
    ctx.drops = timing.get("drops") or {}
    if timing.get("warning"):
        ctx.warnings.append(timing["warning"])
    try:
        by_id, _meta = await ros.ros_for_ids([*my_ids, *extra], league=league, season=season,
                                             week=cweek, db=db)
        ctx.ros = by_id
    except Exception as e:
        logger.warning(f"ROS unavailable for the move planner: {e}")
        ctx.warnings.append("Rest-of-season projections unavailable — lineups and bye checks are empty.")
    if roster_state.get("warning"):
        ctx.warnings.append(roster_state["warning"])
    return ctx, None, roster_state


def parse_goals(goals: list | str | None, default_week: int) -> list[dict]:
    """``[{position, week, kind}]`` from free-text goals ("fill week-7 FLEX hole",
    "DEF for bye", "TE backup") or dicts with those keys."""
    if isinstance(goals, str):
        goals = [goals]
    out = []
    for g in goals or []:
        if isinstance(g, dict):
            given = str(g.get("position") or "").upper()
            out.append({"position": "DEF" if given in ("DST", "D/ST") else given or None,
                        "week": int(g.get("week") or default_week), "kind": g.get("kind") or "fill",
                        "text": str(g.get("goal") or g), "week_given": bool(g.get("week"))})
            continue
        text = str(g)
        m = re.search(r"week[\s-]*(\d+)", text, re.I)
        pos: str | None = None
        for token, canon in (("superflex", "SUPERFLEX"), ("flex", "FLEX"), ("qb", "QB"),
                             ("quarterback", "QB"), ("rb", "RB"), ("wr", "WR"), ("te", "TE"),
                             ("tight end", "TE"), ("kicker", "K"), ("def", "DEF"), ("dst", "DEF"),
                             ("d/st", "DEF"), ("defense", "DEF"), ("k", "K")):
            if re.search(rf"\b{re.escape(token)}\b", text, re.I):
                pos = canon
                break
        kind = "backup" if re.search(r"backup|depth|second", text, re.I) else "fill"
        out.append({"position": pos, "week": int(m.group(1)) if m else default_week, "kind": kind,
                    "text": text, "week_given": bool(m)})
    return out


def _playing(ctx: PlanContext, state: _State, pos: str, week: int) -> int:
    return sum(1 for x in state.active if ctx.position(x) == pos and ctx.playable(x, week))


def _needs(ctx: PlanContext, goals: list[dict], state: _State) -> tuple[list[dict], list[dict]]:
    """``(needs, covered)``: concrete position needs for the goals — else the
    next weeks' empty slots, uncovered single-slot positions and bye-crunch
    starters — and the goals the roster already meets."""
    week = cast(int, ctx.week)
    slots_for = _backup_positions(ctx)
    needs: list[dict] = []
    covered: list[dict] = []
    for g in goals:
        pos = g["position"]
        w = g["week"]
        if pos in ("FLEX", "SUPERFLEX", None):
            lineup = _lineup(ctx, state.active, w, {}, set())
            hole = next((h for h in lineup["holes"] if pos is None or normalize_slot(h["slot"]) == pos), None)
            if hole is None and pos is not None:
                hole = {"eligible": sorted(SLOT_ELIGIBILITY.get(pos, frozenset()))}
            if hole is None:
                covered.append({"goal": g["text"], "why": f"no empty slot in week {w}"})
                continue
            counts = {p: _playing(ctx, state, p, w) for p in hole["eligible"]}
            needs.append({"position": min(hole["eligible"], key=lambda p: counts.get(p, 0)), "week": w,
                          "kind": g["kind"], "goal": g["text"], "positions": list(hole["eligible"])})
            continue
        elif pos in slots_for:
            need = slots_for[pos]
            if "bye" in g["text"].lower() and not g.get("week_given"):
                # "DEF for bye": the first week his position runs short.
                short = [x for x in range(week, ctx.last_regular + 1) if _playing(ctx, state, pos, x) < need]
                if not short:
                    covered.append({"goal": g["text"], "why": f"a {pos} plays every remaining week"})
                    continue
                w = short[0]
            want = need + (1 if g["kind"] == "backup" else 0)
            if _playing(ctx, state, pos, w) >= want:
                covered.append({"goal": g["text"], "why": f"{_playing(ctx, state, pos, w)} {pos} play in week {w}"
                                                          f" for {need} slot(s)"})
                continue
        needs.append({"position": pos, "week": w, "kind": g["kind"], "goal": g["text"]})
    if not goals:
        for w in range(week, min(week + HORIZON_WEEKS, ctx.last_week + 1)):
            lineup = _lineup(ctx, state.active, w, {}, set())
            for h in lineup["holes"]:
                counts = {p: _playing(ctx, state, p, w) for p in h["eligible"]}
                needs.append({"position": min(h["eligible"], key=lambda p: counts.get(p, 0)), "week": w,
                              "kind": "fill", "goal": f"empty {h['slot']} slot in week {w}",
                              "positions": list(h["eligible"])})
            for pos, need in slots_for.items():
                if _playing(ctx, state, pos, w) < need:
                    needs.append({"position": pos, "week": w, "kind": "fill",
                                  "goal": f"no {pos} playing in week {w}"})
        bye = _bye_row(ctx, state.active, week)
        if bye and bye["status"] == "crunch":
            from .bye_week_tools import _positions_to_add
            for pos in _positions_to_add(bye, ctx.slot_list):
                needs.append({"position": pos, "week": week, "kind": "fill",
                              "goal": f"bye crunch in week {week}: lineup {bye['projected_total']} vs "
                                      f"{bye['full_strength_total']} at full strength"})
    seen, unique = set(), []
    for n in needs:
        key = (n["position"], n["week"])
        if key not in seen and n["position"]:
            seen.add(key)
            unique.append(n)
    return unique, covered


def _drop_for(ctx: PlanContext, state: _State, keep: set[str], t: datetime, week: int) -> str | None:
    """The least valuable bench player who can go: never locked, never the
    last body at a single-slot position, never one just added."""
    need = _backup_positions(ctx)
    starting: set[str] = set()
    for w in {week, ctx.week_of(t), cast(int, ctx.week)}:
        names = {row["player"] for row in _lineup(ctx, state.active, w, {}, set())["lineup"]}
        starting |= {p for p in state.active if ctx.name(p) in names}

    def value(p: str) -> float:
        entry = ctx.ros.get(p) or {}
        return float(entry.get("ros_points") or entry.get("total_points") or 0.0)

    candidates = []
    for p in state.active:
        pos = ctx.position(p)
        if p in keep or p in starting or ctx.locked(p, t) is not None:
            continue
        if pos in need and sum(1 for x in state.active if ctx.position(x) == pos) <= need[pos] + 1:
            continue  # keep the backup the planner would otherwise flag
        candidates.append(p)
    return min(candidates, key=value) if candidates else None


async def _candidates(ctx: PlanContext, league_id: str, need: dict) -> list[dict]:
    """Free agents for one need: streamers for DEF/K, waiver targets otherwise."""
    pos, week = need["position"], need["week"]
    out: list[dict] = []
    if pos in ("DEF", "K"):
        from .streaming_tools import get_streaming_options
        res = await get_streaming_options(season=cast(int, ctx.season), start_week=week, weeks_ahead=1,
                                          positions=["DST" if pos == "DEF" else "K"], top_n=5,
                                          league_id=league_id, only_available=True)
        for row in ((res or {}).get("streaming_options") or {}).get("DST" if pos == "DEF" else "K") or []:
            avail = row.get("availability") or {}
            if pos == "DEF" and avail.get("status") == "free_agent":
                out.append({"player": avail.get("unit_player_id") or row.get("team"), "name": row.get("team"),
                            "position": pos, "projected_points": row.get("projected_points"),
                            "source": "get_streaming_options", "stream_score": row.get("stream_score")})
            elif pos == "K":
                fa = next((p for p in avail.get("players") or [] if p.get("status") == "free_agent"), None)
                if fa:
                    out.append({"player": fa["player_id"], "name": fa.get("name"), "position": pos,
                                "projected_points": row.get("projected_points"),
                                "source": "get_streaming_options", "stream_score": row.get("stream_score")})
        return out
    from .waiver_target_tools import get_waiver_targets
    wanted = [p for p in need.get("positions") or [pos] if p not in ("DEF", "K")] or [pos]
    res = await get_waiver_targets(league_id=league_id, roster_id=ctx.roster_id, week=week,
                                   season=ctx.season, positions=wanted, limit=3 * len(wanted))
    for t in (res or {}).get("targets") or []:
        out.append({"player": str(t.get("player_id") or t.get("name")), "name": t.get("name"),
                    "position": t.get("position"), "projected_points": t.get("projected_points"),
                    "upgrade_points": t.get("upgrade_points"),
                    "recommendation": (t.get("waiver_strategy") or {}).get("recommendation"),
                    "suggested_drop": (t.get("drop") or {}).get("player_id"),
                    "source": "get_waiver_targets"})
    return out


def _ensure_info(ctx: PlanContext, pid: str, cand: dict, db) -> None:
    if pid in ctx.info or db is None:
        return
    from .teams import normalize_team
    row = (db.get_athletes_by_ids([pid]) or {}).get(pid) or (
        {"position": "DEF", "team_id": pid} if not pid.isdigit() else {})
    team = normalize_team(row.get("team_id")) or None
    ctx.info[pid] = {"name": row.get("full_name") or cand.get("name") or team or pid,
                     "position": normalize_position(row.get("position")) or cand.get("position"),
                     "team": team, "status": None}


def _gain(ctx: PlanContext, active: list[str], pid: str, drop: str | None, week: int) -> float:
    after = [p for p in active if p != drop] + [pid]
    return round(_lineup(ctx, after, week, {}, set())["total"] - _lineup(ctx, active, week, {}, set())["total"], 1)


async def suggest(ctx: PlanContext, league_id: str, goals: list | str | None,
                  base_moves: list[dict], db=None) -> dict:
    """A minimal valid move set for the goals: at most one move per need, and
    only a pickup who actually raises that week's best lineup.

    Needs more than HORIZON_WEEKS out are listed under ``later`` rather than
    filled now — a bye in week 13 is a pickup for the week-12 waivers.
    """
    week = cast(int, ctx.week)
    parsed = parse_goals(goals, week)
    state = _initial_state(ctx)
    needs, covered = _needs(ctx, parsed, state)
    later = [n for n in needs if n["week"] >= week + HORIZON_WEEKS]
    needs = [n for n in needs if n["week"] < week + HORIZON_WEEKS]
    for n in later:
        n["advice"] = (f"Not yet: add a {n['position']} for week {n['week']} on the waivers after "
                       f"week {n['week'] - 1} (re-run with week={n['week']}).")
    if not needs:
        return {"needs": [], "later": later, "covered": covered, "suggested": [], "rejected": [],
                "message": ("Nothing to do this week or next: " + "; ".join(n["advice"] for n in later)
                            if later else "Every goal is already covered." if covered else
                            f"No empty slot, uncovered QB/TE/K/DEF or bye crunch in weeks {week}-"
                            f"{week + HORIZON_WEEKS - 1}: nothing to add.")}
    results = await asyncio.gather(
        *(asyncio.wait_for(_candidates(ctx, league_id, n), SUGGEST_TIMEOUT_SECONDS) for n in needs),
        return_exceptions=True)
    pool: list[tuple[dict, list[tuple[str, dict]]]] = []
    for need, cands in zip(needs, results, strict=True):
        if isinstance(cands, BaseException):
            pool.append((need, []))
            need["lookup_error"] = type(cands).__name__
            continue
        found = []
        for c in cands:
            pid, _ = resolve_player(ctx, str(c["player"]), db)
            if pid and not state.has(pid) and not ctx.owner_of(pid):
                _ensure_info(ctx, pid, c, db)
                found.append((pid, c))
        pool.append((need, found))
    await _add_ros(ctx, [pid for _, found in pool for pid, _ in found], db)

    proposed: list[dict] = []
    rejected: list[dict] = []
    keep: set[str] = set()
    for need, found in pool:
        if need.get("lookup_error"):
            rejected.append({"need": need, "reason": f"lookup failed: {need['lookup_error']}"})
            continue
        best = None
        tried = []
        for pid, c in found:
            if pid in keep:
                continue
            ws = ctx.waiver_state(pid, ctx.now, None)
            t = ctx.now
            if ws.get("on_waivers") and ws.get("claim_processes_at"):
                t = datetime.fromisoformat(ws["claim_processes_at"])
            elif ctx.locked(pid, ctx.now):
                continue
            if ctx.week_of(t) > need["week"]:
                tried.append(f"{ctx.name(pid)} (claim lands after week {need['week']})")
                continue
            drop = None
            if len(state.active) >= ctx.max_active:
                hint = str(c.get("suggested_drop") or "")
                drop = hint if hint in state.active and ctx.locked(hint, t) is None else \
                    _drop_for(ctx, state, keep | {pid}, t, need["week"])
                if drop is None:
                    tried.append(f"{ctx.name(pid)} (nobody droppable)")
                    continue
            gain = _gain(ctx, state.active, pid, drop, need["week"])
            if gain < SUGGEST_MIN_GAIN:
                tried.append(f"{ctx.name(pid)} ({gain:+.1f} in week {need['week']})")
                continue
            if best is None or gain > best[3]:
                best = (pid, c, ws, gain, drop)
        if best is None:
            rejected.append({"need": need, "reason": "no available player improves the week-"
                             f"{need['week']} lineup by {SUGGEST_MIN_GAIN} pt(s)"
                             + (": " + ", ".join(tried) if tried else "")})
            continue
        pid, c, ws, gain, drop = best
        keep.add(pid)
        if drop:
            state.active.remove(drop)
            keep.add(drop)
        state.active.append(pid)
        move = {"action": "claim" if ws.get("on_waivers") else "add", "player": pid,
                "drop": drop, "when": None, "slot": None, "week": None}
        proposed.append({"move": {**move, "player": ctx.name(pid), "drop": ctx.name(drop) if drop else None},
                         "_ids": move, "need": need, "lineup_gain": gain, "candidate": c})
    return {"needs": needs, "later": later, "covered": covered, "suggested": proposed,
            "rejected": rejected}


async def _add_ros(ctx: PlanContext, ids: list[str], db) -> None:
    """Rest-of-season entries for players a suggestion brought in."""
    from . import ros
    missing = [p for p in ids if p and p not in ctx.ros]
    if not missing:
        return
    try:
        by_id, _ = await ros.ros_for_ids(missing, league=ctx.league, season=cast(int, ctx.season),
                                         week=ctx.current_week, db=db)
        ctx.ros.update(by_id)
    except Exception as e:
        logger.debug(f"ROS for suggested players unavailable: {e}")


async def plan_roster_moves(league_id: str, roster_id: int, moves: list | None = None,
                            week: int | None = None, suggest_moves: bool = False,
                            goals: list | str | None = None, verify: bool = False, db=None) -> dict:
    """See the module docstring and the MCP tool wrapper."""
    from . import sleeper_tools
    from .errors import create_success_response

    moves = list(moves or [])
    if len(moves) > MAX_MOVES:
        return create_success_response({"success": False, "error": f"at most {MAX_MOVES} moves per plan"})
    normalized, errors = [], []
    for raw in moves:
        move, err = normalize_move(raw)
        if err:
            errors.append(err)
        elif move:
            normalized.append(move)
    if errors:
        return create_success_response({"success": False, "error": "; ".join(errors)})
    if not normalized and not suggest_moves:
        return create_success_response({"success": False, "error": "give at least one move, or suggest=True"})

    names = [n for m in normalized for n in (m["player"], m.get("drop")) if n]
    ctx, error, roster_state = await load_context(league_id, roster_id, week, names, db=db,
                                                  purpose="availability")
    if error or ctx is None:
        return create_success_response({"success": False, "error": error})
    from .database import get_shared_db
    db = db if db is not None else get_shared_db()
    header = {"league": {"league_id": league_id, "name": ctx.league.get("name")},
              "roster_id": ctx.roster_id, "season": ctx.season, "current_week": ctx.current_week,
              "week": ctx.week, "now_local": local_time(ctx.now),
              "waiver_rules": {k: v for k, v in waiver_rules(ctx.league).items()
                               if k in ("waiver_type", "processing", "waiver_day", "clear_days")},
              "waiver_position": (ctx.roster.get("settings") or {}).get("waiver_position"),
              "roster_limits": {"active": ctx.max_active, "reserve_slots": ctx.reserve_slots,
                                "ir_eligible_statuses": eligible_statuses(ctx.settings)}}

    if verify:
        resp = await sleeper_tools.get_transactions(league_id, week=ctx.current_week, include_previous_leg=True)
        diff = diff_planned_vs_happened(ctx, normalized, ctx.roster,
                                        (resp or {}).get("transactions") or [], db)
        pending = diff["summary"].get("pending_or_not_entered", 0) + diff["summary"].get("not_happened", 0)
        out = {**header, "mode": "verify", **diff, "warnings": ctx.warnings,
               "message": ("Every planned move is reflected in Sleeper." if not pending and not
                           diff["summary"].get("failed") and not diff["summary"].get("taken_by_other")
                           else "Not everything happened — see each move's status and detail."),
               "note": ("Pending claims never appear in the API: 'pending_or_not_entered' means "
                        "check the app's pending claims — absence here is not proof either way.")}
        return sleeper_tools.mark_roster_staleness(create_success_response(out), roster_state)

    suggestion = None
    if suggest_moves:
        suggestion = await suggest(ctx, league_id, goals, normalized, db)
        normalized = normalized + [s["_ids"] for s in suggestion.get("suggested") or []]
        await _add_ros(ctx, [s["_ids"]["player"] for s in suggestion.get("suggested") or []], db)
        if not normalized:
            return sleeper_tools.mark_roster_staleness(create_success_response({
                **header, "mode": "suggest", "suggestion": {k: v for k, v in suggestion.items() if k != "suggested"},
                "warnings": ctx.warnings,
                "message": suggestion.get("message") or (
                    "No move suggested — " + "; ".join(
                        f"{r['need']['goal']}: {r['reason']}" for r in suggestion.get("rejected") or [])
                    if suggestion.get("rejected") else "Nothing to suggest.")}), roster_state)

    plan = simulate(ctx, normalized, db)
    plan["warnings"] = ctx.warnings + plan["warnings"]
    out = {**header, "mode": "suggest" if suggest_moves else "plan", **plan}
    if suggestion is not None:
        out["suggestion"] = {
            "needs": suggestion.get("needs"),
            "moves": [s["move"] for s in suggestion.get("suggested") or []],
            "why": [{"move": s["move"], "need": s["need"]["goal"], "week": s["need"]["week"],
                     "lineup_gain": s["lineup_gain"], "candidate": s["candidate"]}
                    for s in suggestion.get("suggested") or []],
            "later": suggestion.get("later"),
            "rejected": suggestion.get("rejected"),
            "covered": suggestion.get("covered"),
        }
    bad = [s for s in plan["steps"] if not s["valid"]]
    out["message"] = (
        f"All {len(plan['steps'])} step(s) valid." if not bad else
        f"{len(bad)} of {len(plan['steps'])} step(s) invalid: "
        + "; ".join(f"step {s['step']} ({s['action']} {s['player'].get('name')}): {s.get('reason')}" for s in bad))
    return sleeper_tools.mark_roster_staleness(create_success_response(out), roster_state)


__all__ = [
    "PlanContext", "diff_planned_vs_happened", "failed_claims", "failure_code", "load_context",
    "normalize_move", "parse_goals", "parse_when", "plan_roster_moves", "simulate", "suggest",
]
