"""League scoring model: every Sleeper ``scoring_settings`` key we can price.

Only ``scoring_settings.rec`` used to reach the projections; everything else was
hard-coded (4-pt pass TD, -2 INT, 6-pt rush/rec TD, no fumbles, no bonuses, no
TE premium, flat K and DEF). A 6-point-passing-TD or TE-premium league was
therefore projected as if it were a stock half-PPR one, and the QB/TE ordering
that those settings exist to change never changed.

:class:`ScoringModel` holds the league's weights and prices three kinds of input:

* a real stat line (an nflverse weekly row) — :meth:`ScoringModel.pass_points`,
  :meth:`rush_points`, :meth:`rec_points`; used by the opportunity projection;
* a *typical* per-game line per position — :meth:`bucket_adjust`,
  :meth:`value_multiplier`; used where no per-stat data exists (rank-bucket
  baselines, market values);
* team units — :meth:`kicker_points`, :meth:`expected_defense_points`.

Defaults are Sleeper's defaults. A preset (``"ppr"``, ``"half_ppr"``,
``"standard"``, ``"0.5"``) is the defaults with that reception value, so the
string parameter every tool already has keeps meaning what it meant.

:class:`LeagueScoring` carries a model *through* those string parameters: it is
a ``str`` (the league's exact reception value, e.g. ``"0.5"``) that also holds
the full model, so it can be handed to any existing ``scoring: str`` argument
without changing a signature, and ``resolve_scoring`` recovers the full model
on the other side.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

# Sleeper's default scoring for a new league (reception value set per preset).
SLEEPER_DEFAULTS: dict[str, float] = {
    # Passing
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "pass_2pt": 2.0,
    # Rushing / receiving
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    # Misc offense
    "fum_lost": -2.0, "fum_rec_td": 6.0,
    # Kicking
    "fgm_0_19": 3.0, "fgm_20_29": 3.0, "fgm_30_39": 3.0, "fgm_40_49": 4.0,
    "fgm_50_59": 5.0, "fgm_60p": 6.0, "fgmiss": -1.0, "xpm": 1.0, "xpmiss": -1.0,
    # Team defense
    "def_td": 6.0, "sack": 1.0, "int": 2.0, "fum_rec": 2.0, "safe": 2.0,
    "ff": 1.0, "blk_kick": 2.0, "def_st_td": 6.0, "def_st_ff": 1.0,
    "def_st_fum_rec": 1.0,
    # Special-teams player (return) scoring; not priced, listed for the diff.
    "st_td": 6.0, "st_ff": 1.0, "st_fum_rec": 1.0,
    "pts_allow_0": 10.0, "pts_allow_1_6": 7.0, "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0, "pts_allow_21_27": 0.0, "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
}

# A settings block with all of these is a real Sleeper league: a key it leaves
# out scores nothing there (Sleeper omits zero-valued keys). Anything thinner —
# ``{"rec": 0.5}`` from a caller — is read as overrides on the defaults.
_LEAGUE_CORE_KEYS = frozenset({"pass_yd", "pass_td", "rush_yd", "rush_td", "rec_yd", "rec_td"})

# Every key this module prices. Non-zero league keys outside this set are
# reported as `unmodelled` in `scoring_used` rather than silently ignored.
_PTS_ALLOW_TIERS: tuple[tuple[str, int, int], ...] = (
    ("pts_allow_0", 0, 0), ("pts_allow_1_6", 1, 6), ("pts_allow_7_13", 7, 13),
    ("pts_allow_14_20", 14, 20), ("pts_allow_21_27", 21, 27),
    ("pts_allow_28_34", 28, 34), ("pts_allow_35p", 35, 999),
)
_YDS_ALLOW_TIERS: tuple[tuple[str, int, int], ...] = (
    ("yds_allow_0_100", 0, 99), ("yds_allow_100_199", 100, 199),
    ("yds_allow_200_299", 200, 299), ("yds_allow_300_349", 300, 349),
    ("yds_allow_350_399", 350, 399), ("yds_allow_400_449", 400, 449),
    ("yds_allow_450_499", 450, 499), ("yds_allow_500_549", 500, 549),
    ("yds_allow_550p", 550, 9999),
)
MODELLED_KEYS = frozenset({
    "pass_yd", "pass_td", "pass_int", "pass_2pt", "pass_fd", "pass_cmp", "pass_inc",
    "pass_att", "pass_sack", "bonus_pass_yd_300", "bonus_pass_yd_400",
    "bonus_pass_cmp_25",
    "rush_yd", "rush_td", "rush_2pt", "rush_fd", "rush_att", "bonus_rush_yd_100",
    "bonus_rush_yd_200", "bonus_rush_att_20",
    "rec", "rec_yd", "rec_td", "rec_2pt", "rec_fd", "bonus_rec_rb", "bonus_rec_wr",
    "bonus_rec_te", "bonus_rec_yd_100", "bonus_rec_yd_200",
    "bonus_rush_rec_yd_100", "bonus_rush_rec_yd_200",
    "bonus_fd_qb", "bonus_fd_rb", "bonus_fd_wr", "bonus_fd_te",
    "fum", "fum_lost",
    "fgm", "fgm_yds", "fgm_0_19", "fgm_20_29", "fgm_30_39", "fgm_40_49", "fgm_50p",
    "fgm_50_59", "fgm_60p", "fgmiss", "fgmiss_0_19", "fgmiss_20_29", "fgmiss_30_39",
    "fgmiss_40_49", "fgmiss_50p", "xpm", "xpmiss",
    "def_td", "sack", "int", "fum_rec", "safe", "ff", "blk_kick", "def_st_td",
    "def_st_ff", "def_st_fum_rec", "pts_allow",
    *(k for k, _, _ in _PTS_ALLOW_TIERS), *(k for k, _, _ in _YDS_ALLOW_TIERS),
})


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Typical per-game lines. Where a projection has no per-stat data (rank-bucket
# baselines, market values) the league's settings are priced on these: what a
# fantasy-relevant starter at the position produces in a normal week. `p_*`
# keys are the chance of hitting a game-level yardage/volume bonus.
# --------------------------------------------------------------------------
TYPICAL_LINES: dict[str, dict[str, float]] = {
    "QB": {"attempts": 34, "completions": 22, "passing_yards": 245, "passing_tds": 1.6,
           "interceptions": 0.75, "sacks_suffered": 2.4, "sack_fumbles": 0.25,
           "sack_fumbles_lost": 0.1, "passing_first_downs": 11.5,
           "passing_2pt_conversions": 0.06, "carries": 4, "rushing_yards": 18,
           "rushing_tds": 0.15, "rushing_first_downs": 1.2, "rushing_fumbles": 0.08,
           "rushing_fumbles_lost": 0.04,
           "p_pass_yd_300": 0.2, "p_pass_yd_400": 0.02, "p_pass_cmp_25": 0.3,
           "p_rush_yd_100": 0.003},
    "RB": {"carries": 14, "rushing_yards": 60, "rushing_tds": 0.42,
           "rushing_first_downs": 3.4, "rushing_fumbles": 0.12, "rushing_fumbles_lost": 0.06,
           "rushing_2pt_conversions": 0.02, "targets": 3.8, "receptions": 3.0,
           "receiving_yards": 23, "receiving_tds": 0.1, "receiving_first_downs": 1.1,
           "receiving_fumbles": 0.02, "receiving_fumbles_lost": 0.01,
           "receiving_2pt_conversions": 0.01,
           "p_rush_yd_100": 0.08, "p_rush_yd_200": 0.002, "p_rec_yd_100": 0.003,
           "p_rush_rec_yd_100": 0.15, "p_rush_rec_yd_200": 0.004, "p_rush_att_20": 0.12},
    "WR": {"targets": 7.3, "receptions": 4.5, "receiving_yards": 58, "receiving_tds": 0.38,
           "receiving_first_downs": 2.8, "receiving_fumbles": 0.04,
           "receiving_fumbles_lost": 0.02, "receiving_2pt_conversions": 0.02,
           "carries": 0.3, "rushing_yards": 2,
           "p_rec_yd_100": 0.1, "p_rec_yd_200": 0.002, "p_rush_rec_yd_100": 0.1,
           "p_rush_rec_yd_200": 0.002},
    "TE": {"targets": 5.9, "receptions": 4.0, "receiving_yards": 42, "receiving_tds": 0.32,
           "receiving_first_downs": 2.3, "receiving_fumbles": 0.03,
           "receiving_fumbles_lost": 0.015, "receiving_2pt_conversions": 0.02,
           "p_rec_yd_100": 0.05, "p_rush_rec_yd_100": 0.05},
}
# Opportunity unit each bucket of points is spread over (per target / carry /
# pass attempt), for rebasing the opportunity model's per-opportunity priors.
_BUCKET_VOLUME = {"pass": "attempts", "rush": "carries", "rec": "targets"}

# A league-average kicker's week: made FGs by distance, misses, extra points.
TYPICAL_KICKER: dict[str, float] = {
    "fg_made_0_19": 0.02, "fg_made_20_29": 0.42, "fg_made_30_39": 0.45,
    "fg_made_40_49": 0.45, "fg_made_50_59": 0.28, "fg_made_60_": 0.01,
    "fg_missed_0_19": 0.0, "fg_missed_20_29": 0.02, "fg_missed_30_39": 0.05,
    "fg_missed_40_49": 0.1, "fg_missed_50_59": 0.12, "fg_missed_60_": 0.01,
    "pat_made": 2.3, "pat_missed": 0.1,
}
_FG_BUCKET_YARDS = {"0_19": 19, "20_29": 25, "30_39": 35, "40_49": 45, "50_59": 53, "60_": 61}

# A league-average defense's week at a 22-point opponent total. Sacks and
# takeaways rise as the opponent's expected output falls (see below).
TYPICAL_DEFENSE: dict[str, float] = {
    "sack": 2.4, "int": 0.75, "fum_rec": 0.55, "ff": 0.7, "def_td": 0.12,
    "safe": 0.03, "blk_kick": 0.06, "def_st_td": 0.04, "def_st_ff": 0.05,
    "def_st_fum_rec": 0.05,
}
_AVG_TEAM_TOTAL = 22.0
# Spread of an NFL team's points around its implied total, and of its yards.
_POINTS_SD = 9.5
_YARDS_PER_POINT, _YARDS_SD = 15.0, 75.0


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _tier_probability(lo: int, hi: int, mean: float, sd: float) -> float:
    """P(lo <= X <= hi) for an integer outcome, Normal(mean, sd), floored at 0."""
    upper = _normal_cdf((hi + 0.5 - mean) / sd)
    lower = 0.0 if lo <= 0 else _normal_cdf((lo - 0.5 - mean) / sd)
    return max(0.0, upper - lower)


@dataclass(frozen=True, eq=False)
class ScoringModel:
    """A league's scoring weights, with the pricing functions built on them."""

    weights: Mapping[str, float]
    source: str = "default"          # "league" | "preset" | "default" | "overrides"
    name: str | None = None          # league name, when built from a league
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    # ----- construction ---------------------------------------------------
    @classmethod
    def preset(cls, ppr: float = 1.0, source: str = "preset") -> ScoringModel:
        """Sleeper's defaults with the given points per reception."""
        return cls({**SLEEPER_DEFAULTS, "rec": float(ppr)}, source=source)

    @classmethod
    def from_settings(cls, settings: Mapping | None, name: str | None = None) -> ScoringModel:
        """From a Sleeper ``scoring_settings`` block (or a partial override dict).

        A complete league block is taken as-is — a key it omits scores zero,
        as it does on Sleeper. A partial dict is laid over the defaults.
        """
        clean = {str(k): _f(v) for k, v in (settings or {}).items()
                 if isinstance(v, int | float | str) and _is_number(v)}
        if not clean:
            return cls.preset(1.0, source="default")
        if set(clean) >= _LEAGUE_CORE_KEYS:
            return cls(clean, source="league", name=name)
        return cls({**SLEEPER_DEFAULTS, **clean}, source="overrides", name=name)

    def w(self, key: str) -> float:
        return float(self.weights.get(key, 0.0) or 0.0)

    def has(self, key: str) -> bool:
        return key in self.weights

    @property
    def rec(self) -> float:
        return self.w("rec")

    @property
    def label(self) -> str:
        """Three-way reception label (the lossy one tools report as `scoring`)."""
        if self.rec >= 0.75:
            return "ppr"
        if self.rec >= 0.25:
            return "half_ppr"
        return "standard"

    def reception_value(self, position: str | None) -> float:
        """Points per catch for this position, TE/RB/WR premiums included."""
        pos = (position or "").lower()
        return self.rec + (self.w(f"bonus_rec_{pos}") if pos in ("rb", "wr", "te") else 0.0)

    # ----- real stat lines --------------------------------------------------
    @staticmethod
    def _bonus(g: Mapping, prob_key: str, value: float, threshold: float,
               cap: float | None = None) -> float:
        """1/0 for a real game (value within [threshold, cap)), or the stated
        probability for a typical line carrying `p_*` keys."""
        if prob_key in g:
            return _f(g[prob_key])
        if value >= threshold and (cap is None or value < cap):
            return 1.0
        return 0.0

    def _tiered(self, g, low_key, high_key, value, low, high, p_low, p_high) -> float:
        """Sleeper's non-stacking pair of game bonuses (e.g. 300-399 / 400+)."""
        wl, wh = self.w(low_key), self.w(high_key)
        if not (wl or wh):
            return 0.0
        return (wl * self._bonus(g, p_low, value, low, high if self.has(high_key) else None)
                + wh * self._bonus(g, p_high, value, high))

    def pass_points(self, g: Mapping) -> float:
        att, cmp_ = _f(g.get("attempts")), _f(g.get("completions"))
        yds = _f(g.get("passing_yards"))
        pts = (yds * self.w("pass_yd")
               + _f(g.get("passing_tds")) * self.w("pass_td")
               + _f(g.get("interceptions")) * self.w("pass_int")
               + _f(g.get("passing_2pt_conversions")) * self.w("pass_2pt")
               + _f(g.get("passing_first_downs")) * self.w("pass_fd")
               + _f(g.get("sacks_suffered")) * self.w("pass_sack")
               + _f(g.get("sack_fumbles")) * self.w("fum")
               + _f(g.get("sack_fumbles_lost")) * self.w("fum_lost"))
        if att or cmp_:
            pts += (cmp_ * self.w("pass_cmp") + max(0.0, att - cmp_) * self.w("pass_inc")
                    + att * self.w("pass_att"))
        pts += self._tiered(g, "bonus_pass_yd_300", "bonus_pass_yd_400", yds, 300, 400,
                            "p_pass_yd_300", "p_pass_yd_400")
        if self.w("bonus_pass_cmp_25"):
            pts += self.w("bonus_pass_cmp_25") * self._bonus(g, "p_pass_cmp_25", cmp_, 25)
        return pts

    def rush_points(self, g: Mapping, position: str | None = None) -> float:
        yds, att = _f(g.get("rushing_yards")), _f(g.get("carries"))
        fds = _f(g.get("rushing_first_downs"))
        pos = (position or "").lower()
        pts = (yds * self.w("rush_yd")
               + _f(g.get("rushing_tds")) * self.w("rush_td")
               + _f(g.get("rushing_2pt_conversions")) * self.w("rush_2pt")
               + fds * (self.w("rush_fd") + (self.w(f"bonus_fd_{pos}") if pos else 0.0))
               + att * self.w("rush_att")
               + _f(g.get("rushing_fumbles")) * self.w("fum")
               + _f(g.get("rushing_fumbles_lost")) * self.w("fum_lost"))
        pts += self._tiered(g, "bonus_rush_yd_100", "bonus_rush_yd_200", yds, 100, 200,
                            "p_rush_yd_100", "p_rush_yd_200")
        if self.w("bonus_rush_att_20"):
            pts += self.w("bonus_rush_att_20") * self._bonus(g, "p_rush_att_20", att, 20)
        # The combined rush+rec yardage bonus is booked here: it is one game
        # bonus and has to live in exactly one bucket.
        total = yds + _f(g.get("receiving_yards"))
        pts += self._tiered(g, "bonus_rush_rec_yd_100", "bonus_rush_rec_yd_200", total,
                            100, 200, "p_rush_rec_yd_100", "p_rush_rec_yd_200")
        return pts

    def rec_points(self, g: Mapping, position: str | None = None) -> float:
        yds = _f(g.get("receiving_yards"))
        pos = (position or "").lower()
        fd_value = self.w("rec_fd") + (self.w(f"bonus_fd_{pos}") if pos else 0.0)
        pts = (_f(g.get("receptions")) * self.reception_value(position)
               + yds * self.w("rec_yd")
               + _f(g.get("receiving_tds")) * self.w("rec_td")
               + _f(g.get("receiving_2pt_conversions")) * self.w("rec_2pt")
               + _f(g.get("receiving_first_downs")) * fd_value
               + _f(g.get("receiving_fumbles")) * self.w("fum")
               + _f(g.get("receiving_fumbles_lost")) * self.w("fum_lost"))
        pts += self._tiered(g, "bonus_rec_yd_100", "bonus_rec_yd_200", yds, 100, 200,
                            "p_rec_yd_100", "p_rec_yd_200")
        return pts

    def game_points(self, g: Mapping, position: str | None = None) -> float:
        """Fantasy points for one offensive stat line in this league."""
        return self.pass_points(g) + self.rush_points(g, position) + self.rec_points(g, position)

    # ----- typical lines (no per-stat data) --------------------------------
    def _reference(self) -> ScoringModel:
        """Sleeper defaults at this league's reception value — the scale the
        rank buckets and market values are already rebased to."""
        ref = self._cache.get("ref")
        if ref is None:
            ref = ScoringModel.preset(self.rec)
            self._cache["ref"] = ref
        return ref

    def bucket_adjust(self, position: str | None) -> float:
        """Share of a *full-PPR default* baseline this league adds beyond the
        reception value (which callers already rebase for). 0.0 for presets.

        A 0.5 TE premium is worth half a point per catch on ~4 catches, i.e.
        ~20% of a typical TE week; that is what a rank-bucket TE gains.
        """
        pos = (position or "").upper()
        line = TYPICAL_LINES.get(pos)
        if not line:
            return 0.0
        full = ScoringModel.preset(1.0).game_points(line, pos)
        if full <= 0:
            return 0.0
        return (self.game_points(line, pos) - self._reference().game_points(line, pos)) / full

    def value_multiplier(self, position: str | None) -> float:
        """This league's typical-week points over a default league at the same
        reception value: the correction a market value (priced for a stock
        league of this PPR) needs. 1.0 for presets and for K/DEF."""
        pos = (position or "").upper()
        line = TYPICAL_LINES.get(pos)
        if not line:
            return 1.0
        ref = self._reference().game_points(line, pos)
        return max(0.0, self.game_points(line, pos) / ref) if ref > 0 else 1.0

    def prior_delta(self, position: str | None, bucket: str) -> float:
        """Per-opportunity change to the opportunity model's position prior
        (per target / carry / attempt) versus a default league at this PPR."""
        pos = (position or "").upper()
        line = TYPICAL_LINES.get(pos)
        volume = _f((line or {}).get(_BUCKET_VOLUME[bucket]))
        if not line or volume <= 0:
            return 0.0
        fn = {"pass": lambda m: m.pass_points(line),
              "rush": lambda m: m.rush_points(line, pos),
              "rec": lambda m: m.rec_points(line, pos)}[bucket]
        return (fn(self) - fn(self._reference())) / volume

    # ----- kickers -----------------------------------------------------------
    def _fg_made_value(self, bucket: str) -> float:
        flat = self.w("fgm")
        if bucket in ("0_19", "20_29", "30_39", "40_49"):
            return flat + self.w(f"fgm_{bucket}")
        if bucket == "50_59":
            return flat + (self.w("fgm_50_59") if self.has("fgm_50_59") else self.w("fgm_50p"))
        # 60+: its own key, else whichever 50+ key the league uses.
        for key in ("fgm_60p", "fgm_50_59", "fgm_50p"):
            if self.has(key):
                return flat + self.w(key)
        return flat

    def _fg_miss_value(self, bucket: str) -> float:
        key = "fgmiss_50p" if bucket in ("50_59", "60_") else f"fgmiss_{bucket}"
        return self.w("fgmiss") + self.w(key)

    def kicker_points(self, line: Mapping) -> float:
        """Points for a kicker stat line (nflverse `fg_made_*` / `pat_*` columns)."""
        pts = 0.0
        for bucket, yards in _FG_BUCKET_YARDS.items():
            made = _f(line.get(f"fg_made_{bucket}"))
            pts += made * (self._fg_made_value(bucket) + yards * self.w("fgm_yds"))
            pts += _f(line.get(f"fg_missed_{bucket}")) * self._fg_miss_value(bucket)
        pts += _f(line.get("pat_made")) * self.w("xpm") + _f(line.get("pat_missed")) * self.w("xpmiss")
        return pts

    def kicker_scale(self) -> float:
        """This league's typical kicker week over Sleeper's default one."""
        default = ScoringModel.preset(1.0).kicker_points(TYPICAL_KICKER)
        return max(0.0, self.kicker_points(TYPICAL_KICKER) / default) if default else 1.0

    # ----- team defense ------------------------------------------------------
    def expected_defense_points(self, opponent_total: float | None) -> float:
        """Expected DEF points given the opponent's implied total.

        Points allowed ~ Normal(total, 9.5) priced through the league's tiers
        (and a per-point `pts_allow` if it has one); yards allowed likewise.
        Sacks and takeaways scale up as the opponent is expected to struggle.
        """
        mean = _AVG_TEAM_TOTAL if opponent_total is None else max(3.0, float(opponent_total))
        pts = self.w("pts_allow") * mean
        for key, lo, hi in _PTS_ALLOW_TIERS:
            if self.w(key):
                pts += self.w(key) * _tier_probability(lo, hi, mean, _POINTS_SD)
        yards_mean = mean * _YARDS_PER_POINT
        for key, lo, hi in _YDS_ALLOW_TIERS:
            if self.w(key):
                pts += self.w(key) * _tier_probability(lo, hi, yards_mean, _YARDS_SD)
        pressure = min(1.4, max(0.7, _AVG_TEAM_TOTAL / mean))
        for key, rate in TYPICAL_DEFENSE.items():
            scaled = rate * pressure if key in ("sack", "int", "fum_rec", "ff", "def_td") else rate
            pts += scaled * self.w(key)
        return pts

    def defense_scale(self, opponent_total: float | None) -> float:
        """This league's expected DEF week over Sleeper's default one."""
        default = ScoringModel.preset(1.0).expected_defense_points(opponent_total)
        if default <= 0:
            return 1.0
        return max(0.0, self.expected_defense_points(opponent_total) / default)

    # ----- reporting ---------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        """A short, stable id for these weights: equal scoring, equal id.

        Built from the non-zero weights only, so a league block that spells
        out a zero and one that omits the key (which also scores zero) agree.
        Stored projections are keyed by it: two leagues with the same
        reception value but different pass-yard or sack weights project
        differently and must not read each other's numbers.
        """
        if "fingerprint" not in self._cache:
            items = sorted((k, round(float(v), 4)) for k, v in self.weights.items()
                           if abs(float(v or 0.0)) > 1e-9)
            text = ";".join(f"{k}={v:g}" for k, v in items)
            self._cache["fingerprint"] = hashlib.sha1(text.encode()).hexdigest()[:12]
        return self._cache["fingerprint"]

    def non_default(self) -> dict[str, dict[str, float]]:
        """Keys whose weight differs from Sleeper's default (reception value
        excluded — it is reported on its own)."""
        keys = (set(self.weights) | set(SLEEPER_DEFAULTS)) - {"rec"}
        out = {}
        for k in sorted(keys):
            mine, default = self.w(k), float(SLEEPER_DEFAULTS.get(k, 0.0))
            # A league on the older single 50+ FG key is not "missing" 50-59/60+.
            if k in ("fgm_50_59", "fgm_60p") and not self.has(k) and self.has("fgm_50p"):
                continue
            if abs(mine - default) > 1e-9:
                out[k] = {"league": mine, "default": default}
        return out

    def unmodelled(self) -> list[str]:
        """Non-zero league keys no projection prices (return yards, pick-sixes…)."""
        return sorted(k for k, v in self.weights.items() if v and k not in MODELLED_KEYS)

    def summary(self) -> dict:
        """The `scoring_used` block tools report."""
        out = {
            "source": self.source,
            "label": self.label,
            "rec": self.rec,
            "non_default": self.non_default(),
            "unmodelled": self.unmodelled(),
        }
        if self.name:
            out["league"] = self.name
        premiums = {p: self.w(f"bonus_rec_{p}") for p in ("rb", "wr", "te") if self.w(f"bonus_rec_{p}")}
        if premiums:
            out["reception_premiums"] = premiums
        return out


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


class LeagueScoring(str):
    """A scoring *string* that also carries the league's full model.

    Its string value is the league's exact per-reception value (``"0.5"``), so
    it drops into every existing ``scoring: str`` parameter, response field and
    ``scoring_to_ppr`` call unchanged; :func:`resolve_scoring` reads the model
    back out. Any string operation returns a plain ``str`` and so degrades to
    the reception-value preset, never to something wrong.
    """

    model: ScoringModel

    def __new__(cls, model: ScoringModel):
        rec = model.rec
        obj = super().__new__(cls, str(float(rec)))
        obj.model = model
        return obj

    def __reduce__(self):  # keep pickling/copying from dropping the model
        return (LeagueScoring, (self.model,))


def scoring_label_to_ppr(scoring) -> float:
    """Parse a scoring label/number to points per reception (see player_values)."""
    from .player_values import scoring_to_ppr
    return scoring_to_ppr(scoring)


def resolve_scoring(scoring=None) -> ScoringModel:
    """Any scoring argument -> a :class:`ScoringModel`.

    Accepts a model, a :class:`LeagueScoring`, a Sleeper ``scoring_settings``
    dict, a league dict (with ``scoring_settings``), a number, or a label
    (``ppr`` / ``half_ppr`` / ``standard`` / ``"0.5"``) -> preset.
    """
    if isinstance(scoring, ScoringModel):
        return scoring
    if isinstance(scoring, LeagueScoring):
        return scoring.model
    if isinstance(scoring, Mapping):
        if "scoring_settings" in scoring:
            return ScoringModel.from_settings(scoring.get("scoring_settings"), scoring.get("name"))
        return ScoringModel.from_settings(scoring)
    if scoring is None:
        return ScoringModel.preset(1.0, source="default")
    if isinstance(scoring, int | float):
        return ScoringModel.preset(float(scoring))
    return ScoringModel.preset(scoring_label_to_ppr(scoring))


def league_scoring(league: Mapping | None) -> LeagueScoring:
    """The scoring argument to pass on for a Sleeper league dict."""
    league = league or {}
    return LeagueScoring(ScoringModel.from_settings(league.get("scoring_settings"),
                                                    league.get("name")))


def scoring_fingerprint(scoring) -> str:
    """`ScoringModel.fingerprint` for any scoring argument."""
    return resolve_scoring(scoring).fingerprint


def scoring_used(scoring) -> dict:
    """`scoring_used` summary for any scoring argument."""
    return resolve_scoring(scoring).summary()

