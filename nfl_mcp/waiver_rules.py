"""What a league's waiver settings actually mean for "can I add him now?".

Sleeper exposes the raw knobs (``waiver_type``, ``daily_waivers``,
``waiver_day_of_week``, ``waiver_clear_days``, ``daily_waivers_days``) and
nothing that answers the only question a manager has before kickoff: is this an
instant add, or a claim that processes later — and when?

This module reports the settings in interpreted form and stops there. It does
not answer "instant or claim?", because that was attempted twice in live use
and was wrong both times:

1. "Never dropped, therefore not on waivers, therefore instant" — false under
   weekly waivers, where the whole free-agent pool is locked during the week.
2. "`daily_waivers=1`, therefore instant" — also false; the league's own app
   showed the claim processing on the waiver day anyway. Which days daily
   waivers actually run is encoded in `daily_waivers_days` as a bitmask, and
   the observed processing time (~09:05 CEST) matches none of the exposed
   fields.

So the honest output is the configuration plus a pointer to the app, which
shows the real answer per player. Inventing the verdict here is what caused the
wrong advice in the first place.
"""
from __future__ import annotations

# Sleeper's `waiver_day_of_week`, verified against a league whose app shows
# Wednesday for the value 2.
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

FAAB = "faab"
PRIORITY = "priority"


def waiver_rules(league: dict | None) -> dict:
    """Interpret a league's waiver configuration.

    Returns:
        waiver_type: "faab" or "priority"
        processing: "daily" or "weekly"
        waiver_day: weekday name for weekly processing, else None
        clear_days: days a dropped player stays on waivers
        note: what the configuration implies, and what it does not settle
        how_to_confirm: where the authoritative answer lives

    Deliberately does NOT return an "instant vs claim" verdict — see the
    comment at the return statement.
    """
    settings = (league or {}).get("settings") or {}
    budget = settings.get("waiver_budget") or 0
    is_faab = settings.get("waiver_type") == 2 and budget > 0
    daily = bool(settings.get("daily_waivers"))
    day_index = settings.get("waiver_day_of_week")
    day = _WEEKDAYS[day_index] if isinstance(day_index, int) and 0 <= day_index < 7 else None

    if daily:
        note = (
            "Daily waivers are enabled, but which days they run is encoded in "
            f"`daily_waivers_days` ({settings.get('daily_waivers_days')}) as a "
            "bitmask this module does not decode. An add may still be a claim "
            f"processed on {day or 'the waiver day'}."
        )
    else:
        note = (
            "Weekly waivers: during the game week the whole free-agent pool is "
            f"locked, so every add is a claim processed on {day or 'the waiver day'} "
            "— being unowned all season does not make a player an instant add."
        )

    return {
        "waiver_type": FAAB if is_faab else PRIORITY,
        "processing": "daily" if daily else "weekly",
        "waiver_day": day,
        "clear_days": settings.get("waiver_clear_days"),
        "budget": budget if is_faab else None,
        "daily_waivers_days": settings.get("daily_waivers_days") if daily else None,
        "note": note,
        # Deliberately no `add_mode` field. Deriving "instant vs claim" from
        # these settings was tried twice in live use and was wrong both times —
        # once by reasoning "never dropped, therefore instant" (false under
        # weekly waivers), once by reading `daily_waivers=1` as instant (the
        # league's own app showed the claim processing on the waiver day
        # regardless). Asserting it here would reproduce that error inside the
        # fix meant to prevent it.
        "how_to_confirm": (
            "Whether a specific add is instant or a claim — and when a claim "
            "processes — is shown in the league app on the add dialog and under "
            "pending claims. Neither is derivable from these settings, and "
            "pending claims are not exposed by the API at all, so their absence "
            "from a transaction list does not mean none exist."
        ),
    }
