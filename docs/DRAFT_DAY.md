# Draft-Day Playbook

How to actually draft with this server — before, during, and how to read what it
tells you. Distilled from a full live run against a real Sleeper draft.

> TL;DR: **rounds 1–8, follow the tool** (value / VBD, cliffs, runs, "elite scarce
> position early or wait on QB"). **Bench rounds, your judgment takes over**
> (RB/WR depth + handcuffs, byes) — pure value goes blind on deep benches.

---

## 1. Before the draft

```bash
# (a) Flight check — validate the whole flow against your real league, so nothing
#     surprises you on the clock.
python -m evals.live.validate_draft --username your_sleeper_name --season 2026

# (b) Study the board — VBD-ranked, tiered, format-aware.
#     (via your assistant / MCP client)
get_draft_board(scoring="ppr", num_teams=12)

# (c) Rehearse — 100 mock drafts from your slot to learn your realistic roster shape.
simulate_draft(my_slot=7, num_teams=12, num_sims=100)
```

Get a **live `draft_id`** to practise against: start a **Sleeper mock draft**
(app/website, solo-with-CPU works year-round). The id is in the URL:
`sleeper.com/draft/nfl/<draft_id>`. Note your **slot** (draft position).

---

## 2. During the draft

Two ways — both read the **live** Sleeper draft state:

**A) Ask your assistant, each pick** (nothing to run):
> *"I'm in draft `<draft_id>` at slot 4 — who should I take now?"*
> → calls `recommend_draft_pick(draft_id, my_slot)`.

**B) Live watcher** (a terminal "war room"):
```bash
python -m evals.live.draft_watch --draft-id <draft_id> --my-slot 4
```
It polls the draft and, each time you're on the clock, prints your roster, what's
gone since your last pick, the top picks (with value cliffs & positional runs),
and — once your starters are full — flips to a **bench-depth overlay**. Make your
pick in Sleeper; it watches for your next turn. `--once` for a single check.

> **Sleeper API lag:** picks can take a second or two to appear in the API. If the
> tool seems a beat behind, re-run — `recommend_draft_pick` always re-fetches.

---

## 3. Reading the recommendation

| Signal | Meaning | Use it to… |
|--------|---------|-----------|
| **VBD** | value over positional replacement | rank who's actually worth most *for a draft* |
| **need-weighted** order | VBD × your roster need | the tool's suggested pick given your team |
| **value cliff** | steep drop to the next player at that position | grab *this* player now or lose the tier |
| **positional run** | many of one position going recently | that position is thinning — react |
| **tier** | consensus tier | tier breaks = natural "reach vs wait" lines |

---

## 4. Strategy — where the tool leads, where you lead

**Rounds 1–8 → follow the tool.** It's built for value here:
- Take the **best value** (highest need-weighted VBD), not the flashiest name.
- **Elite scarce position early** (an elite TE/RB) *or* **wait on QB** — in 1-QB
  leagues, elite QBs fall; you often get a WR/RB *and* an elite QB by waiting.
- Respect **value cliffs** (grab the last player before a drop) and **runs**.

**Bench rounds → your judgment.** Pure VBD is *blind* on deep benches (it can rank
a backup QB or a 4th TE over a useful RB/WR body). Your rules:
- **RB/WR depth first**, especially **handcuffs to your studs** (a stud RB going
  down with no backup is how seasons end).
- **Don't over-stack** one position (chasing "value" into a 3rd/4th TE leaves you
  thin where injuries actually happen — RB/WR).
- **1 backup QB** only in the last round (bye-week fill), or skip it.
- The watcher flags this automatically as **BENCH MODE**.

---

## 5. Worked example (real live mock, 12-team PPR, slot 4)

| Rd | Pick | Why |
|----|------|-----|
| 1 | **Bijan Robinson** (RB) | Elite RB fell to 1.04 — steal; RB cliff after him |
| 2 | **Saquon Barkley** (RB) | Top value at the turn → elite RB duo |
| 3 | **A.J. Brown** (WR) | Best WR with a **cliff after him**; WR1 secured |
| 4 | **Tyler Warren** (TE) | Top value + locks a scarce position |
| 5 | **Zay Flowers** (WR) | WR2 with a cliff — QB deliberately deferred |
| 6 | **Patrick Mahomes** (QB) | **Wait paid off**: got Flowers *and* an elite QB at 6.09 |
| 7–8 | Kittle, Kelce (TE) | High value for FLEX — but note: this went TE-heavy |
| 9–11 | White, Diggs, Mason (RB/WR) | **Bench mode**: overrode the tool's TE/QB picks for RB/WR depth |

Lessons baked in: the value engine built an **A-grade starting core**; the
**3-TE drift** in rounds 7–8 is exactly why the bench rounds need the human
depth/handcuff overlay.

---

## 6. Full-power config (in-season)

For the weekly tools that build on the draft (projections, start/sit, FAAB), run
the server with:
```bash
-e NFL_MCP_ADVANCED_ENRICH=1   # real snap%/usage enrichment
-e NFL_MCP_PREFETCH=1          # warm caches
-e ODDS_API_KEY=...            # live Vegas game environment (optional)
```
See the main [README](../README.md#-going-live--use-it-from-your-ai-client).
