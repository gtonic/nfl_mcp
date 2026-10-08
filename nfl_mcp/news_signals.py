"""News-text signals: what the reports say about a player's role.

The injury table already stores a news blurb for most players
(``player_injuries.injury_description``: the ESPN / Rotowire-style line, one
per player, replaced as news arrives), and coaches' words in it carry role
information the usage numbers show only a week or two later: "didn't get
another carry after his fumble", "the two backs are 'gonna rotate'",
"remains week-to-week", "designated to return to practice", "expected to be
the lead back". This reads them with a small rule-based classifier
(:data:`PATTERNS`, phrase lists, negation-aware) into flags:

    benched                 lost his job / the work: benched, no more touches
                            after a fumble, "watched X dominate touches",
                            snaps that dwindled
    committee               a rotation, a split backfield, a timeshare
    lead_role               the lead back, the starter, the primary option,
                            "dominated the touches"
    limited_snaps           a snap count, eased back in, a managed workload
    week_to_week            an injury measured in weeks, not days
    designated_to_return    a reserve-list (IR / PUP / NFI) player whose
                            21-day window opened / who was activated
    practice_progress       back at practice ("resumed practicing",
                            "returned to practice") -- this week's news
    expected_to_play        will play despite the injury tag
    unlikely_to_play        "not expected to play"
    ruled_out               ruled out / won't play / inactive with an injury
    inactive_healthy_scratch  "(coach's decision) inactive", a healthy
                            scratch -- reported only (a backup's routine)

Each flag keeps its source snippet, the report date and a recency weight
(``0.5 ** (age_days / HALF_LIFE_DAYS)``; nothing older than
``MAX_AGE_DAYS``). A blurb is attributed clause by clause to the player it is
about -- the nearest player name before the phrase, among the team's names,
else the blurb's own player -- so "Keenum ... backup role after Johnson said
Tyson Bagent will start" is a lead role for Bagent, not for Keenum, and a
teammate's blurb that names him counts for him too; a role named as a title
("bell-cow running back Jonathan Taylor") is the named player's. A phrase
inside a condition ("if Hall can't go, Allen would be the lead back", "if
he's not cleared to play") is ``conditional``: a conditional lead role
counts at ``CONDITIONAL_WEIGHT`` (the handcuff's path to the job), the other
conditional role and availability flags not at all. Precision / recall on
hand-labelled stored sentences: ``python -m evals.news_classifier_eval``.

Since schema v19 the blurbs are not only the injury table's one line per
player: every item of the news sources (`news_sources`: ESPN's fantasy
player feed, NBC Sports / Rotoworld, CBS) is kept in ``player_news``, and
:func:`build_index` reads all of a player's recent items (``news=``) next to
his injury blurb. The same note from two sources (or the injury blurb, which
is ESPN's copy of the RotoWire note) is read once (:func:`text_key`), and a
flag is counted once per player whatever the number of items saying it
(:func:`signals_for`: the most recent wins), so more sources add coverage,
not weight. Sources are not weighted against each other: the ESPN and CBS
items are RotoWire's, the NBC ones Rotoworld's, both beat-reporter
summaries with no accuracy record yet to tell them apart. Availability flags
(``expected_to_play`` / ``unlikely_to_play`` / ``ruled_out``) are about one
game: only items since the week rolled over (:func:`week_start`, Tuesday)
count, and only the newest of them ("unlikely" Wednesday, "expected to play"
Friday: expected to play); ``practice_progress`` and
``inactive_healthy_scratch`` are this week's too (``GAME_FLAGS``). Each flag carries its ``source``, ``url`` and
``snippet``.

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
# the same span (see `classify`). Measured against the hand-labelled
# sentences in ``tests/fixtures/news_labels.jsonl``
# (``python -m evals.news_classifier_eval``; thresholds in
# ``tests/test_news_classifier_eval.py``).
_DAY = r"(?:sunday|monday|thursday|saturday|friday|tonight|this week|week \d+)"
PATTERNS: dict[str, tuple[str, ...]] = {
    # A healthy scratch: inactive by the coach's choice, not an injury.
    # For a backup (the third QB, the fifth receiver) it is every week's
    # routine; reported, never priced (the gameday inactive list,
    # `gameday_inactives`, settles a starter's game).
    "inactive_healthy_scratch": (
        r"\bhealthy scratch\b",
        r"\(coach's decision\)",
    ),
    "ruled_out": (
        r"(?<!prior to being )(?<!before being )\bruled out\b"
        r"(?! for the (?:rest|remainder))(?! of bounds)",
        r"\b(?:won't|will not) play\b",
        rf"\bwill miss (?:{_DAY}|the team's|another|(?:a|his|their) (?:second|third|fourth|fifth|"
        r"sixth|\w+ straight|\w+ consecutive))",
        # "Mims (foot) is listed as inactive": inactive with an injury.
        r"\((?!coach)[^)]{2,40}\)\s+(?:(?:is|was|will be|has been)\s+)?(?:listed as\s+)?inactive\b",
        r"\binactive status has (?:now )?been confirmed\b",
    ),
    "unlikely_to_play": (
        r"\b(?:not|isn't|is not|aren't|unlikely) (?:expected |likely |going |set )?to (?:play|suit up)\b",
        r"\b(?:only )?an? (?:outside|slim|small|long-?shot) (?:chance|shot) (?:to|of|at) "
        r"(?:play|suit|go)",
        # "likely to miss a second straight game" -- not "several weeks"
        # (week_to_week's) nor "unlikely to miss".
        r"(?<!un)(?<!not )\blikely to (?:sit out|miss)\b(?!\s+(?:multiple|several|a few|a couple|"
        r"significant|extended|time|the rest|the remainder|\w+ weeks|weeks))",
        r"\bmore likely than not (?:to |he will |that he will )?(?:sit out|miss)\b",
        rf"\bwill likely (?:sit out|miss) (?:{_DAY}|the|his|a|another)\b",
    ),
    "expected_to_play": (
        r"(?<!before being )(?<!after being )\b(?:expected|set|on track|slated|poised|cleared|"
        r"plans?|good|ready) to (?:play|suit up|go)\b",
        r"\bwill play\b(?!\s+(?:week \d+\s+)?with)",
        r"\bcleared (?:from |out of )?(?:the )?concussion protocol\b",
        r"\bin the clear for\b",
        r"\bbe able to (?:play|suit up|go)\b",
        rf"\b(?:likely|should|will) be ready (?:to go\b|for (?:{_DAY}|the|his|this))",
        r"\bexpect (?:him|[a-z'\-]+) to (?:play|suit up)\b",
        r"\bbetter than (?:50/50|a coin flip) to (?:play|suit up)\b",
        r"\bon track to avoid an? (?:injury )?designation\b",
        rf"\b(?:poised|set|expected|on track|slated) to return (?:to action|to the lineup|{_DAY})",
    ),
    "benched": (
        r"\bbenched\b",
        r"\b(?:didn't|did not|never|wouldn't|would not) (?:get|see|receive|touch) "
        r"(?:another|a single|any more|any other|the ball again)",
        r"\b(?:didn't|did not|never) see the field (?:again|the rest of|after|from that point)",
        r"\bsaw his (?:snaps|playing time|role|work(?:load)?|touches|carries) "
        r"(?:dwindle|shrink|diminish|decrease|drop|evaporate|disappear)",
        # "lost a fumble ... and watched Monangai dominate touches": the
        # watcher lost the work (the teammate's lead role is the next phrase).
        r"\bwatched\b(?=\s+(?-i:(?:[A-Z][\w'.\-]+\s+){1,2})(?:dominate|take over|handle|soak up|get))",
        r"\b(?:lost|losing) (?:work|snaps|carries|touches|playing time|his role) (?:to|after)\b",
        r"\b(?:pulled|yanked|sat down) (?:after|following) (?:his|a) (?:lost )?(?:fumble|drop|turnover)",
        r"\bdemoted\b",
        r"\blost (?:his|the) (?:starting|lead|top|no\. 1|first-team) (?:job|role|spot|gig)\b",
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
        r"\bceded (?:[\w-]+ ){0,4}(?:reps|carries|snaps|touches|work)\b",
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
        r"\bmake the (?:week \d+ )?start\b",
        r"\bprimary (?:ball ?carrier|back|option|target|receiver)\b",
        r"\bdominat(?:e|es|ed|ing) (?:the )?(?:touches|carries|backfield (?:work|touches|snaps)|"
        r"work(?:load)?|looks|targets|snaps)\b",
    ),
    "limited_snaps": (
        r"\blimited (?:snaps|snap count|workload|role|reps|number of snaps)\b",
        r"\bon a (?:snap|pitch) count\b",
        r"\bpitch count\b",
        r"\b(?:snap count|workload|snaps|reps) (?:will be |is |could be |may be |being )?"
        r"(?:managed|monitored|limited|restricted)\b",
        r"\bmanag(?:e|ed|ing) (?:his|their) (?:snaps|workload|reps|snap count)\b",
        r"\b(?:well )?(?:under|less than) a full workload\b",
        r"\beas(?:e|ed|ing) (?:\w+ )?back\b",
    ),
    "week_to_week": (
        # An injury measured in weeks -- not a workload that varies "from
        # week to week", a "week-to-week role", "more day-to-day than week-to-week".
        r"(?<!from )(?<!vary )(?<!varies )(?<!varied )(?<!than )(?<!elevated )(?<!significant )"
        r"\bweek[- ]to[- ]week\b(?!\s+(?:role|basis|value|production|usage|floor|outlook|fantasy|"
        r"option|start|play|consistency|volatility|output))",
        r"\bmiss(?:es|ing)?\s+(?:multiple|several|a few|a couple(?: of)?)\s+"
        r"(?:more\s+)?(?:games|weeks)\b",
    ),
    # A reserve-list (IR / PUP / NFI) player whose return has begun: the
    # 21-day practice window opened, designated to return, activated. Not a
    # plain "returned to practice" -- that is `practice_progress`.
    "designated_to_return": (
        r"\bdesignated (?:[\w'.\-()/,]+ ){0,4}(?:for|to) return\b",
        r"\bpractice window\b",
        r"\bactivated (?:[\w'.\-]+ ){0,3}(?:from|off) (?:the )?(?:injured reserve|ir\b|pup\b|nfi\b|"
        r"physically unable|reserve)",
        r"\breinstated (?:[\w'.\-]+ ){0,3}(?:from|off) (?:the )?(?:injured reserve|ir\b|pup\b|nfi\b|"
        r"reserve)",
    ),
    # Back at practice after missing it ("resumed practicing", "returned to
    # practice"): this week's injury news, not a reserve-list return.
    "practice_progress": (
        r"\b(?:returned|returns) to (?:practice|the practice field)\b",
        r"\bresum(?:ed|es) (?:practicing|practice|on-field work|working out|team drills)\b",
        r"\bback at practice\b",
    ),
}
# Flags a negation just before the phrase cancels ("won't be benched",
# "isn't expected to start", "hasn't been ruled out", "has yet to be ruled
# out"). `unlikely_to_play` carries its negation in the phrase itself.
NEGATABLE = {"benched", "committee", "lead_role", "limited_snaps", "expected_to_play",
             "week_to_week", "ruled_out", "designated_to_return", "practice_progress",
             "inactive_healthy_scratch"}
# A lead role the sentence only asks about ("to get a sense of who among
# the duo will be Chicago's lead runner") is not one.
_QUESTION_RE = re.compile(r"\b(?:who|whether|which|wonder(?:s|ed|ing)?)\b[^.;]*$", re.I)
_NEGATION_RE = re.compile(r"(?:\b(?:not|no|never|without|yet to)\b|n't\b)[^.;,]{0,25}$", re.I)
_COMPILED = {flag: tuple(re.compile(p, re.I) for p in pats) for flag, pats in PATTERNS.items()}

# Conditionals: "if Hall can't go, Allen would be the lead back", "Warren
# could be primed for a workhorse role", "if he's not cleared to play". A
# hit in one is marked ``conditional``: build_index keeps a conditional
# lead role at CONDITIONAL_WEIGHT (the handcuff's path to the job) and drops
# the other conditional role / availability flags. A snap count "if he
# suits up" is still a snap count: limited_snaps, week_to_week and the
# reserve / practice flags are read whatever the condition.
CONDITIONAL_FLAGS = {"lead_role", "committee", "benched", "ruled_out", "unlikely_to_play",
                     "expected_to_play", "inactive_healthy_scratch"}
CONDITIONAL_KEEP = {"lead_role"}
CONDITIONAL_WEIGHT = 0.5
# "if" / "unless" / "in the event" / "assuming" before the phrase -- not an
# indirect question ("it's unclear if", "to see if").
_IF_BEFORE_RE = re.compile(
    r"(?<!unclear )(?<!unknown )(?<!see )(?<!wonder )(?<!determine )(?<!sure )(?<!clear )"
    r"(?<!know )(?<!tell )(?<!ask )(?<!even )"
    r"\b(?:if|unless|in the event|assuming|should (?:he|[A-Z][a-z]+) (?:miss|sit|be))\b", re.I)
# "would"/"could"/"potentially" before the phrase in its clause: with an
# "if" after it ("... a workhorse role if Spears is ruled out"), or right
# before it ("would put Thompson on track to play").
_MODAL_RE = re.compile(r"\b(?:would|could|might|potentially)\b", re.I)
_MODAL_NEAR_RE = re.compile(r"\b(?:would|could|might)\b[^,;]{0,40}$", re.I)
_IF_AFTER_RE = re.compile(r"\b(?:if|unless|should he)\b", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"[;:]|\s--\s|,\s(?:but|and|while|though|although)\s", re.I)

# The designated-to-return phrases (strict: a reserve-list return), and the
# cues that a player who is out is on his way back: those plus a plain
# return to practice. `projections._teammate_return_games` reads the second
# for a teammate who is unavailable (Out / IR), where "returned to
# practice" is the window opening; `value_trajectory` reads the flags.
DESIGNATED_RE = re.compile("|".join(PATTERNS["designated_to_return"]), re.I)
RETURN_CUE_RE = re.compile("|".join(PATTERNS["designated_to_return"]
                                    + PATTERNS["practice_progress"]), re.I)

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
    "practice_progress": {"model_mult": 1.0, "confidence": 0},
    "inactive_healthy_scratch": {"model_mult": 1.0, "confidence": 0},
}
# Flags whose multiplier `role_shift`'s lost role already prices (the model
# reweights its volume from the break week): confidence only then.
ROLE_LOSS_FLAGS = {"benched", "committee"}
# Availability flags: they say nothing about a player already listed Out.
AVAILABILITY_FLAGS = {"expected_to_play", "unlikely_to_play", "ruled_out"}
# Flags about one week's game (read from this week's items only): the
# availability flags, a return to practice, a healthy scratch.
GAME_FLAGS = AVAILABILITY_FLAGS | {"practice_progress", "inactive_healthy_scratch"}
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
# Quotation marks around a quoted word ("only an \"outside chance\" of
# playing", 'appears "unlikely" to play') would break the phrase patterns.
_QUOTES_RE = re.compile("[\"\u201c\u201d]")
# The NFL week rolls over on Tuesday morning (US Eastern): availability
# flags from before it are about last week's game.
WEEK_ROLLOVER_WEEKDAY = 1  # Tuesday
WEEK_ROLLOVER_HOUR_UTC = 9
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


_TITLE_NAME_RE = re.compile(r"\s+(?:running back\s+|RB\s+|back\s+)?"
                            r"((?-i:[A-Z][\w.'\-]+))(?:\s+((?-i:[A-Z][\w.'\-]+)))?")


def _title_subject(after: str, names: dict[str, str | None]) -> str | None:
    """A role named as a title before the player ("bell-cow running back
    Jonathan Taylor", "lead back Breece Hall"): the player right after it."""
    m = _TITLE_NAME_RE.match(after)
    if not m:
        return None
    first, last = m.group(1).rstrip("."), (m.group(2) or "").rstrip(".")
    if last and names.get(f"{first} {last}".lower()):
        return names[f"{first} {last}".lower()]
    for word in (last, first):
        key = re.sub(r"'s?$", "", (word or "").lower())
        if key and names.get(key):
            return names[key]
    return None


def _conditional(sentence: str, start: int, end: int) -> bool:
    """Whether the phrase at ``sentence[start:end]`` is inside a condition
    (see ``CONDITIONAL_FLAGS``)."""
    before, after = sentence[:start], sentence[end:]
    if _IF_BEFORE_RE.search(before):
        return True
    clause = _CLAUSE_SPLIT_RE.split(before)[-1]
    if _MODAL_NEAR_RE.search(clause):
        return True
    next_clause = _CLAUSE_SPLIT_RE.split(after)[0]
    return bool(_MODAL_RE.search(clause) and _IF_AFTER_RE.search(next_clause))


def classify(text: str | None, owner: str = "", names: dict[str, str | None] | None = None,
             owner_last: str = "") -> list[dict]:
    """``[{flag, about, snippet, conditional?}]`` for one blurb.

    `owner` is the key of the player the blurb is filed under (his last name
    `owner_last`); `names` maps his teammates' last names to their keys
    (None for a name two of them share), for attribution. Without `names`
    everything is the owner's; a clause about an ambiguous name is dropped.
    A role named as a title ("bell-cow running back Jonathan Taylor") is the
    named player's. A hit inside a condition ("if Hall can't go, Allen would
    be the lead back") carries ``conditional: True`` (``CONDITIONAL_FLAGS``);
    an unconditional hit of the same flag and player replaces it.
    """
    names = names or {}
    out: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    text = _QUOTES_RE.sub("", (text or "").replace("\u2019", "'"))
    for sentence in _SENTENCE_RE.split(text):
        taken: list[tuple[int, int]] = []
        for flag, pats in _COMPILED.items():
            for pat in pats:
                for m in pat.finditer(sentence):
                    if any(a < m.end() and m.start() < b for a, b in taken):
                        continue  # a more specific flag owns this span
                    before = sentence[:m.start()]
                    if flag == "lead_role" and _QUESTION_RE.search(before):
                        taken.append((m.start(), m.end()))
                        continue
                    if flag in NEGATABLE and _NEGATION_RE.search(before):
                        if flag == "expected_to_play":
                            flag_out = "unlikely_to_play"
                        else:
                            taken.append((m.start(), m.end()))
                            continue
                    else:
                        flag_out = flag
                    taken.append((m.start(), m.end()))
                    about = None
                    if flag_out == "lead_role":
                        about = _title_subject(sentence[m.end():], names)
                    about = about or _subject(before, owner, names, owner_last)
                    if about is None:
                        continue
                    conditional = (flag_out in CONDITIONAL_FLAGS
                                   and _conditional(sentence, m.start(), m.end()))
                    prior = seen.get((flag_out, about))
                    if prior is not None and (conditional or not prior.get("conditional")):
                        continue
                    hit = {"flag": flag_out, "about": about, "snippet": sentence.strip()[:240],
                           **({"conditional": True} if conditional else {})}
                    if prior is not None:
                        out.remove(prior)
                    seen[(flag_out, about)] = hit
                    out.append(hit)
    return out


def week_start(now: datetime | None = None) -> datetime:
    """When the current NFL week began: the last Tuesday rollover."""
    from datetime import timedelta
    now = now or datetime.now(UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    start = (now - timedelta(days=(now.weekday() - WEEK_ROLLOVER_WEEKDAY) % 7)).replace(
        hour=WEEK_ROLLOVER_HOUR_UTC, minute=0, second=0, microsecond=0)
    return start if start <= now else start - timedelta(days=7)


def text_key(text: str | None) -> str:
    """A note's identity across sources: its first words, normalized."""
    words = re.findall(r"[a-z0-9]+", _QUOTES_RE.sub("", (text or "").lower()))
    return " ".join(words[:30])


def news_rows(items: list[dict] | None) -> list[dict]:
    """``player_news`` rows in the shape :func:`build_index` reads."""
    from .news_sources import item_text
    out = []
    for it in items or []:
        text = item_text(it)
        if not text or not it.get("player_name"):
            continue
        out.append({"player_name": it["player_name"], "team_id": it.get("team"),
                    "text": text, "date_reported": it.get("published_at") or it.get("recorded_at"),
                    "source": it.get("source"), "url": it.get("url"),
                    "headline": it.get("headline")})
    return out


def recent_news(db, now: datetime | None = None, teams: list[str] | None = None) -> list[dict]:
    """The stored news items young enough to be read (``MAX_AGE_DAYS``), as
    :func:`build_index` rows. [] without a database or the table."""
    from datetime import timedelta
    if db is None or not hasattr(db, "get_player_news"):
        return []
    since = ((now or datetime.now(UTC)) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    try:
        return news_rows(db.get_player_news(since=since, teams=teams))
    except Exception as e:
        logger.debug(f"stored news unavailable: {e}")
        return []


# Reading every stored item takes a few hundred milliseconds (~1000 items, a
# week in season) and a weekly briefing projects several rosters: the index
# is reused while neither the reports nor the news changed, for this long.
INDEX_CACHE_SECONDS = 300
_index_cache: dict = {}


def index_for(db, rows: list[dict] | None) -> dict[tuple[str, str], list[dict]]:
    """:func:`build_index` over the report rows and the stored news, now;
    cached ``INDEX_CACHE_SECONDS`` per database while both are unchanged."""
    import time
    news = recent_news(db)
    rows = rows or []
    key = (str(getattr(db, "db_path", id(db))), len(rows),
           max((str(r.get("updated_at") or r.get("date_reported") or "") for r in rows),
               default=""),
           len(news), max((str(n.get("date_reported") or "") for n in news), default=""))
    hit = _index_cache.get("entry")
    if hit and hit[0] == key and time.monotonic() - hit[1] < INDEX_CACHE_SECONDS:
        return hit[2]
    index = build_index(rows, news=news)
    _index_cache["entry"] = (key, time.monotonic(), index)
    return index


def build_index(rows: list[dict] | None, now: datetime | None = None,
                extra: list[dict] | None = None,
                news: list[dict] | None = None) -> dict[tuple[str, str], list[dict]]:
    """``{(normalized name, team): [signal]}`` from stored report rows.

    `rows` are ``player_injuries`` rows (``player_name``, ``team_id``,
    ``injury_description``, ``date_reported``, ``sources``); `extra` takes
    the same shape for any other text (``text`` instead of
    ``injury_description``), e.g. items from ``get_nfl_news``; `news` the
    stored news items (:func:`news_rows` / :func:`recent_news`), read first
    so a note both carry keeps the news item's link. Each signal: ``{flag,
    weight, snippet, date_reported, source, url, from_player}``.
    """
    from .teams import normalize_team
    now = now or datetime.now(UTC)
    rollover = week_start(now)
    items = []
    seen_text: set[tuple[str, str, str]] = set()
    # Every listed player's last name, for attribution -- not only the ones
    # with a fresh blurb: a teammate's blurb can name a player whose own is old.
    by_team: dict[str, dict[str, str | None]] = {}
    for r in [*(news or []), *(rows or []), *(extra or [])]:
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
        dup = (key, team, text_key(text))
        if dup in seen_text:  # the same note from another source
            continue
        seen_text.add(dup)
        items.append((name, team, text, weight, r))
    index: dict[tuple[str, str], list[dict]] = {}
    for name, team, text, weight, r in items:
        owner = _norm(name)
        when = _parse_date(r.get("date_reported"))
        last_week = when is not None and when < rollover
        for hit in classify(text, owner, by_team.get(team), _last_name(name)):
            if hit["flag"] in GAME_FLAGS and last_week:
                continue  # about last week's game
            conditional = bool(hit.get("conditional"))
            if conditional and hit["flag"] not in CONDITIONAL_KEEP:
                continue  # "if he's not cleared to play"
            source = r.get("source") or r.get("sources") or "injury_report"
            index.setdefault((hit["about"], team), []).append({
                "flag": hit["flag"],
                "weight": round(weight * CONDITIONAL_WEIGHT, 3) if conditional else weight,
                "snippet": hit["snippet"],
                **({"conditional": True} if conditional else {}),
                "date_reported": r.get("date_reported"),
                "source": source if isinstance(source, str) else str(source),
                **({"url": r["url"]} if r.get("url") else {}),
                **({"from_player": name} if hit["about"] != owner else {}),
            })
    return index


def signals_for(index: dict | None, name: str | None, team: str | None) -> list[dict]:
    """The flags for one player, strongest (most recent) first, one per flag.

    Of the availability flags only the most recent one is kept: a later
    "expected to play" settles an earlier "unlikely to play" (and vice versa).
    """
    from .teams import normalize_team
    if not index or not name:
        return []
    found = index.get((_norm(name), normalize_team(team) or (team or "").upper())) or []
    best: dict[str, dict] = {}
    for s in sorted(found, key=lambda s: -s["weight"]):
        best.setdefault(s["flag"], s)
    availability = [f for f in best.values() if f["flag"] in AVAILABILITY_FLAGS]
    if len(availability) > 1:
        newest = max(availability, key=lambda f: (
            _parse_date(f.get("date_reported")) or datetime.min.replace(tzinfo=UTC),
            f["weight"]))
        best = {k: v for k, v in best.items() if k not in AVAILABILITY_FLAGS or v is newest}
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
    """The news flags for one player from the stored reports and news items
    (his own and teammates' that name him). Never raises; [] without a
    database."""
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
    news = recent_news(db, now, teams=[canon])
    return signals_for(build_index(rows, now, news=news), name, canon)


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
