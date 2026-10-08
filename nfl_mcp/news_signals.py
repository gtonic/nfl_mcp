"""News-text signals: what the reports say about a player's role.

The injury table already stores a news blurb for most players
(``player_injuries.injury_description``: the ESPN / Rotowire-style line, one
per player, replaced as news arrives), and coaches' words in it carry role
information the usage numbers show only a week or two later: "didn't get
another carry after his fumble", "the two backs are 'gonna rotate'",
"remains week-to-week", "designated to return to practice", "expected to be
the lead back". This reads them with a small rule-based classifier
(:data:`PATTERNS`, phrase lists, negation-aware) into flags:

    benched                 lost his job / no more touches after a mistake
    committee               a rotation, a split backfield, a timeshare
    lead_role               the lead back, the starter, the primary option
    limited_snaps           a snap count, eased back in, a managed workload
    week_to_week            an injury measured in weeks, not days
    designated_to_return    a reserve-list player whose window has opened
    expected_to_play        will play despite the injury tag
    unlikely_to_play        "not expected to play"
    ruled_out               ruled out / won't play this week

Each flag keeps its source snippet, the report date and a recency weight
(``0.5 ** (age_days / HALF_LIFE_DAYS)``; nothing older than
``MAX_AGE_DAYS``). A blurb is attributed clause by clause to the player it is
about -- the nearest player name before the phrase, among the team's names,
else the blurb's own player -- so "Keenum ... backup role after Johnson said
Tyson Bagent will start" is a lead role for Bagent, not for Keenum, and a
teammate's blurb that names him counts for him too.

What the flags change (:func:`adjustment`) is deliberately small and only
on our model's share of the blend: Sleeper's projections are Rotowire's,
whose writers also wrote the blurb, so their stat line has already seen the
news; our model only sees volume. ``benched`` / ``committee`` /
``limited_snaps`` take a bounded multiplier and a confidence cut;
``lead_role`` / ``expected_to_play`` add confidence; the rest are reported.
When `role_shift` already found a lost role the model's volume is
reweighted from the break week, so a ``benched`` / ``committee`` flag then
moves confidence only. Not backtested yet, so the weights are kept mild:
the live table keeps the latest blurb per player, but every distinct blurb
is now kept in ``injury_news_history`` (schema v17) and
``evals/backtest/signal_history.py`` measures these effects once a few weeks
are collected.

:func:`player_news` is the entry point for other modules (one player, from
the database); :func:`build_index` / :func:`signals_for` serve a batch.
:func:`role_security` folds the role read, the news and teammate context
into one score for waiver and drop decisions. Pure apart from
:func:`player_news`; no network.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Recency: a blurb's weight halves every HALF_LIFE_DAYS; older than
# MAX_AGE_DAYS it is not read at all (the role it describes has had a game or
# two to show up in the usage numbers by then).
HALF_LIFE_DAYS = 4.0
MAX_AGE_DAYS = 10.0
# An undated blurb counts at this weight.
UNDATED_WEIGHT = 0.5

# Flag -> regexes (case-insensitive). Ordered so a more specific flag wins
# the same span (see `_classify_sentence`).
PATTERNS: dict[str, tuple[str, ...]] = {
    "ruled_out": (
        r"\bruled out\b(?! for the (?:rest|remainder))",
        r"\b(?:won't|will not) play\b",
        r"\bwill miss (?:sunday|monday|thursday|saturday|this week|week \d+|the team's)",
    ),
    "unlikely_to_play": (
        r"\b(?:not|isn't|is not|aren't|unlikely) (?:expected |likely |going |set )?to (?:play|suit up)\b",
        r"\b(?:only )?an? (?:outside|slim|small|long-?shot) chance (?:to|of) (?:play|suit up|go)",
    ),
    "expected_to_play": (
        r"\b(?:expected|set|on track|slated|poised|cleared|plans?|good|ready) to (?:play|suit up|go)\b",
        r"\bwill play\b",
        r"\bcleared (?:from |out of )?(?:the )?concussion protocol\b",
    ),
    "benched": (
        r"\bbenched\b",
        r"\b(?:didn't|did not|never|wouldn't|would not) (?:get|see|receive|record|touch) "
        r"(?:another|a single|any more|any other|the ball again)",
        r"\bdemoted\b",
        r"\blost (?:his|the) (?:starting|lead|top|no\. 1|first-team) (?:job|role|spot|gig)\b",
        r"\bhealthy scratch\b",
        r"\(coach's decision\)",
    ),
    "committee": (
        r"\bcommittee\b",
        r"\brotat(?:e|es|ed|ing|ion)\b",
        r"\bsplit(?:s|ting)? (?:the )?(?:work(?:load)?|carries|reps|snaps|touches|backfield)\b",
        r"\bshar(?:e|es|ed|ing) (?:the )?(?:backfield|carries|work(?:load)?|snaps|reps|touches)\b",
        r"\btime[- ]?share\b",
        r"\b1a\b",
        r"\bhot hand\b",
        r"\bplatoon\b",
    ),
    "lead_role": (
        r"\blead (?:back|role|runner|rusher|ball ?carrier)\b",
        r"\bbell[- ]?cow\b",
        r"\bworkhorse\b",
        r"\bfeatured? (?:back|role)\b",
        r"\b(?:expected|set|slated|in line|poised|going|projected|named) to (?:start|be the "
        r"(?:starter|lead|primary|top|no\. 1))\b",
        r"\bwill (?:start|be the starter)\b",
        r"\bnamed (?:the )?starter\b",
        r"\b(?:trending toward|in line for|line up for) (?:the |a )?start\b",
        r"\bprimary (?:ball ?carrier|back|option|target|receiver)\b",
    ),
    "limited_snaps": (
        r"\blimited (?:snaps|snap count|workload|role|reps|number of snaps)\b",
        r"\bsnap count\b",
        r"\bpitch count\b",
        r"\beas(?:e|ed|ing) (?:\w+ )?back\b",
        r"\bworkload (?:will be |is |could be )?(?:managed|monitored|limited)\b",
    ),
    "week_to_week": (
        r"\bweek[- ]to[- ]week\b",
        r"\bmiss(?:es|ing)?\s+(?:multiple|several|a few|a couple(?: of)?)\s+"
        r"(?:more\s+)?(?:games|weeks)\b",
    ),
    "designated_to_return": (
        r"designated (?:for|to) return",
        r"return(?:ed|s)? to practice",
        r"practice window",
    ),
}
# Flags a negation just before the phrase cancels ("won't be benched",
# "isn't expected to start"). `ruled_out` / `unlikely_to_play` carry their
# negation in the phrase itself.
NEGATABLE = {"benched", "committee", "lead_role", "limited_snaps", "expected_to_play",
             "week_to_week"}
_NEGATION_RE = re.compile(r"(?:\b(?:not|no|never|without)\b|n't\b)[^.;,]{0,25}$", re.I)
_COMPILED = {flag: tuple(re.compile(p, re.I) for p in pats) for flag, pats in PATTERNS.items()}
# The designated-to-return phrases, shared with the returning-teammate logic
# (`projections._teammate_return_games`).
DESIGNATED_RE = re.compile("|".join(PATTERNS["designated_to_return"]), re.I)

# What a flag moves, at full recency weight. ``model_mult`` is on our
# model's share of the blend only (see module doc); ``confidence`` is added
# to the projection's confidence. Heuristic: the blurb history
# (`injury_news_history`, schema v17) had no flagged player-week with a
# trailing rate of 5+ points in 2026 weeks 3-4 (491 unflagged), so there is
# nothing to fit yet. Re-calibrate once it does:
# `python -m evals.backtest.signal_history --db nfl_data.db --season 2026 --truth sleeper`.
EFFECTS: dict[str, dict[str, float]] = {
    "benched": {"model_mult": 0.85, "confidence": -8},
    "committee": {"model_mult": 0.93, "confidence": -5},
    "limited_snaps": {"model_mult": 0.90, "confidence": -5},
    "lead_role": {"model_mult": 1.0, "confidence": 5},
    "week_to_week": {"model_mult": 1.0, "confidence": -5},
    "expected_to_play": {"model_mult": 1.0, "confidence": 5},
    "unlikely_to_play": {"model_mult": 1.0, "confidence": -5},
    "ruled_out": {"model_mult": 1.0, "confidence": -5},
    "designated_to_return": {"model_mult": 1.0, "confidence": 0},
}
# Flags whose multiplier `role_shift`'s lost role already prices (the model
# reweights its volume from the break week): confidence only then.
ROLE_LOSS_FLAGS = {"benched", "committee"}
# Availability flags: they say nothing about a player already listed Out.
AVAILABILITY_FLAGS = {"expected_to_play", "unlikely_to_play", "ruled_out"}
MIN_MODEL_MULT = 0.80
MAX_CONFIDENCE_SWING = 10

# Role security (waivers, drops): points per flag at full recency weight,
# plus the role read and the teammate context. Positive is a role that is
# growing or safe, negative one that is shrinking or borrowed.
SECURITY_POINTS: dict[str, float] = {
    "lead_role": 1.0,
    "committee": -1.0,
    "benched": -1.5,
    "limited_snaps": -0.5,
    "week_to_week": -0.5,
}
ROLE_TREND_POINTS = {"role_up": 1.0, "role_down": -1.0}
# Volume borrowed from a teammate who is out now (inherited) or was out for
# part of his window and is back or due back (returning teammates).
BORROWED_POINTS = -1.0
SECURITY_RANGE = 2.0

_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _norm(name: str | None) -> str:
    from .opportunity_tools import norm_name
    return norm_name(name or "")


def _last_name(name: str | None) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    while len(parts) > 1 and parts[-1].lower().strip(".") in ("jr", "sr", "ii", "iii", "iv", "v"):
        parts.pop()
    return parts[-1].lower() if parts else ""


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def recency_weight(reported, now: datetime | None = None) -> float:
    """0..1: the blurb's weight by age (``UNDATED_WEIGHT`` when undated)."""
    when = _parse_date(reported)
    if when is None:
        return UNDATED_WEIGHT
    age = max(0.0, ((now or datetime.now(UTC)) - when).total_seconds() / 86400)
    if age > MAX_AGE_DAYS:
        return 0.0
    return round(0.5 ** (age / HALF_LIFE_DAYS), 3)


def _subject(before: str, owner: str, names: dict[str, str | None],
             owner_last: str = "") -> str | None:
    """Whose clause it is: the last known last name before the phrase in the
    sentence (``names``: last name -> player key, None when two teammates
    share it), else the blurb's owner. None when that name is ambiguous."""
    last: str | None = owner
    prev = ""
    for m in _WORD_RE.finditer(before):
        word = m.group(0).lower()
        word = word[:-2] if word.endswith("'s") else word.rstrip("'")
        full, prev = f"{prev} {word}", word
        if names.get(full):  # "Lamar Jackson" settles a shared last name
            last = names[full]
            continue
        if word not in names and word != owner_last:
            continue
        # "head coach Ben Johnson": a coach who shares a player's name.
        if re.search(r"\bcoach\s+\w+\s*$", before[:m.start()], re.I):
            continue
        last = owner if word == owner_last else names[word]
    return last


def classify(text: str | None, owner: str = "", names: dict[str, str | None] | None = None,
             owner_last: str = "") -> list[dict]:
    """``[{flag, about, snippet}]`` for one blurb.

    `owner` is the key of the player the blurb is filed under (his last name
    `owner_last`); `names` maps his teammates' last names to their keys
    (None for a name two of them share), for attribution. Without `names`
    everything is the owner's; a clause about an ambiguous name is dropped.
    """
    names = names or {}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for sentence in _SENTENCE_RE.split(text or ""):
        taken: list[tuple[int, int]] = []
        for flag, pats in _COMPILED.items():
            for pat in pats:
                for m in pat.finditer(sentence):
                    if any(a < m.end() and m.start() < b for a, b in taken):
                        continue  # a more specific flag owns this span
                    before = sentence[:m.start()]
                    if flag in NEGATABLE and _NEGATION_RE.search(before):
                        if flag == "expected_to_play":
                            flag_out = "unlikely_to_play"
                        else:
                            taken.append((m.start(), m.end()))
                            continue
                    else:
                        flag_out = flag
                    taken.append((m.start(), m.end()))
                    about = _subject(before, owner, names, owner_last)
                    if about is None or (flag_out, about) in seen:
                        continue
                    seen.add((flag_out, about))
                    out.append({"flag": flag_out, "about": about,
                                "snippet": sentence.strip()[:240]})
    return out


def build_index(rows: list[dict] | None, now: datetime | None = None,
                extra: list[dict] | None = None) -> dict[tuple[str, str], list[dict]]:
    """``{(normalized name, team): [signal]}`` from stored report rows.

    `rows` are ``player_injuries`` rows (``player_name``, ``team_id``,
    ``injury_description``, ``date_reported``, ``sources``); `extra` takes
    the same shape for any other text (``text`` instead of
    ``injury_description``), e.g. items from ``get_nfl_news``. Each signal:
    ``{flag, weight, snippet, date_reported, source, from_player}``.
    """
    from .teams import normalize_team
    now = now or datetime.now(UTC)
    items = []
    # Every listed player's last name, for attribution -- not only the ones
    # with a fresh blurb: a teammate's blurb can name a player whose own is old.
    by_team: dict[str, dict[str, str | None]] = {}
    for r in [*(rows or []), *(extra or [])]:
        text = r.get("injury_description") or r.get("text")
        name = r.get("player_name") or r.get("name")
        team = normalize_team(r.get("team_id") or r.get("team")) or ""
        if not name:
            continue
        last, key = _last_name(name), _norm(name)
        if last:
            names = by_team.setdefault(team, {})
            names[last] = key if names.get(last, key) == key else None
            names[" ".join(str(name).lower().split()[:2])] = key
        if not text:
            continue
        weight = recency_weight(r.get("date_reported"), now)
        if weight <= 0:
            continue
        items.append((name, team, text, weight, r))
    index: dict[tuple[str, str], list[dict]] = {}
    for name, team, text, weight, r in items:
        owner = _norm(name)
        for hit in classify(text, owner, by_team.get(team), _last_name(name)):
            source = r.get("source") or r.get("sources") or "injury_report"
            index.setdefault((hit["about"], team), []).append({
                "flag": hit["flag"], "weight": weight, "snippet": hit["snippet"],
                "date_reported": r.get("date_reported"),
                "source": source if isinstance(source, str) else str(source),
                **({"from_player": name} if hit["about"] != owner else {}),
            })
    return index


def signals_for(index: dict | None, name: str | None, team: str | None) -> list[dict]:
    """The flags for one player, strongest (most recent) first, one per flag."""
    from .teams import normalize_team
    if not index or not name:
        return []
    found = index.get((_norm(name), normalize_team(team) or (team or "").upper())) or []
    best: dict[str, dict] = {}
    for s in sorted(found, key=lambda s: -s["weight"]):
        best.setdefault(s["flag"], s)
    return list(best.values())


def adjustment(flags: list[dict] | None, role_trend: str | None = None,
               availability: str | None = None) -> dict:
    """``{model_mult, confidence_delta, applied}``: what the flags change.

    Weighted by recency (a flag at weight w moves ``w`` of its effect),
    bounded (``MIN_MODEL_MULT``, ``±MAX_CONFIDENCE_SWING``). A lost role
    already found by `role_shift` keeps ``ROLE_LOSS_FLAGS`` to confidence;
    availability flags are ignored for a player already Out.
    """
    mult, conf, applied = 1.0, 0.0, []
    for f in flags or []:
        effect = EFFECTS.get(f["flag"])
        if not effect:
            continue
        if f["flag"] in AVAILABILITY_FLAGS and availability == "out":
            continue
        w = float(f.get("weight") or 0.0)
        m = float(effect["model_mult"])
        if f["flag"] in ROLE_LOSS_FLAGS and role_trend == "role_down":
            m = 1.0
        mult *= 1.0 - (1.0 - m) * w
        conf += effect["confidence"] * w
        if m != 1.0 or effect["confidence"]:
            applied.append(f["flag"])
    return {"model_mult": round(max(MIN_MODEL_MULT, min(1.0, mult)), 3),
            "confidence_delta": round(max(-MAX_CONFIDENCE_SWING,
                                          min(MAX_CONFIDENCE_SWING, conf))),
            "applied": applied}


def player_news(db, name: str, team: str | None, now: datetime | None = None) -> list[dict]:
    """The news flags for one player from the stored reports (his own blurb
    and teammates' that name him). Never raises; [] without a database."""
    from .teams import normalize_team
    if db is None or not name:
        return []
    try:
        rows = db.get_all_current_injuries() or []
    except Exception as e:
        logger.debug(f"news signals unavailable: {e}")
        return []
    canon = normalize_team(team) or ""
    rows = [r for r in rows if (normalize_team(r.get("team_id")) or "") == canon]
    return signals_for(build_index(rows, now), name, canon)


def role_security(proj: dict | None) -> dict:
    """``{score, label, reasons}``: how safe a player's role is, from a
    projection row -- its role read (``role_trend``), news flags
    (``news_flags``) and whether his volume is borrowed from a teammate who
    is out (``breakdown.vacated_volume``) or back / due back
    (``breakdown.returning_teammates``). ``score`` is in
    ``±SECURITY_RANGE``; label rising / secure / neutral / shaky / shrinking.
    """
    proj = proj or {}
    bd = proj.get("breakdown") or {}
    score, reasons = 0.0, []
    trend = proj.get("role_trend")
    if trend in ROLE_TREND_POINTS:
        score += ROLE_TREND_POINTS[trend]
        detail = "; ".join(proj.get("role_flags") or [])
        reasons.append(f"{trend.replace('_', ' ')}" + (f" ({detail})" if detail else ""))
    for f in proj.get("news_flags") or []:
        pts = SECURITY_POINTS.get(f.get("flag"))
        if pts:
            w = float(f.get("weight") or 0.0)
            score += pts * w
            reasons.append(f"news: {f['flag'].replace('_', ' ')} — \"{f.get('snippet', '')[:90]}\"")
    if bd.get("vacated_volume"):
        score += BORROWED_POINTS
        who = ", ".join(bd.get("starters_out_ahead") or []) or "a teammate"
        reasons.append(f"volume borrowed from {who} (out)")
    returning = bd.get("returning_teammates") or []
    if returning:
        score += BORROWED_POINTS
        reasons.append("trailing volume inflated while " + ", ".join(
            r.get("name", "?") for r in returning) + " missed games (back or due back)")
    score = round(max(-SECURITY_RANGE, min(SECURITY_RANGE, score)), 2)
    label = ("rising" if score >= 1 else "secure" if score > 0 else "neutral" if score == 0
             else "shaky" if score > -1 else "shrinking")
    return {"score": score, "label": label, "reasons": reasons}
