"""Precision / recall of the news-text classifier (`nfl_mcp.news_signals`)
against the hand-labelled sentences in ``tests/fixtures/news_labels.jsonl``.

Each line: ``{id, split, source, owner, team, mates, text, labels: [[flag,
player]]}`` -- one sentence from the stored news (``player_news``,
``injury_news_history``, preseason to week 5 2026), the player it is filed
under, the teammates it names, and the flags a reader takes from it, each
attributed to the player it is about. ``split``: ``dev`` sentences were read
while the patterns were written (a sample stratified by the old classifier's
hits and by cue words -- fumble, inactive, if, practice, start); ``holdout``
ones were drawn afterwards and labelled before the classifier saw them --
the honest number. A conditional ("if Hall can't go, Allen would be the
lead back") is not a flag; the classifier marks it ``conditional`` and it is
not counted here.

    python -m evals.news_classifier_eval [--fixture PATH]

prints the per-flag table; ``tests/test_news_classifier_eval.py`` holds the
thresholds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "news_labels.jsonl"


def load(path: Path | str = FIXTURE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def predict(entry: dict, classify=None) -> set[tuple[str, str]]:
    """``{(flag, player name)}`` the classifier reads from one entry (its
    conditional hits left out), attributed as `news_signals.build_index` does."""
    from nfl_mcp import news_signals as ns
    classify = classify or ns.classify
    owner = entry["owner"]
    key_to_name = {ns._norm(owner): owner}
    names: dict[str, str | None] = {}
    for n in [owner, *entry.get("mates", [])]:
        key = ns._norm(n)
        key_to_name[key] = n
        last = ns._last_name(n)
        names[last] = key if names.get(last, key) == key else None
        names[" ".join(n.lower().split()[:2])] = key
    hits = classify(entry["text"], ns._norm(owner), names, ns._last_name(owner))
    return {(h["flag"], key_to_name.get(h["about"], h["about"])) for h in hits
            if not h.get("conditional")}


def score(entries: list[dict], classify=None) -> dict[str, dict]:
    """``{flag: {tp, fp, fn, precision, recall}}`` plus ``_all`` (micro)."""
    counts: dict[str, dict[str, int]] = {}
    for e in entries:
        gold = {(f, who) for f, who in e["labels"]}
        pred = predict(e, classify)
        for f, _ in gold | pred:
            counts.setdefault(f, {"tp": 0, "fp": 0, "fn": 0})
        for item in pred & gold:
            counts[item[0]]["tp"] += 1
        for item in pred - gold:
            counts[item[0]]["fp"] += 1
        for item in gold - pred:
            counts[item[0]]["fn"] += 1
    total = {k: sum(c[k] for c in counts.values()) for k in ("tp", "fp", "fn")}
    out = {}
    for flag, c in [*sorted(counts.items()), ("_all", total)]:
        p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
        r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
        out[flag] = {**c, "precision": None if p is None else round(p, 3),
                     "recall": None if r is None else round(r, 3)}
    return out


def table(result: dict[str, dict]) -> str:
    def pct(v):
        return "  -  " if v is None else f"{v:5.2f}"
    lines = [f"{'flag':26} {'tp':>4} {'fp':>4} {'fn':>4}  precision  recall"]
    for flag, c in result.items():
        lines.append(f"{flag:26} {c['tp']:4d} {c['fp']:4d} {c['fn']:4d}  "
                     f"{pct(c['precision']):>9}  {pct(c['recall']):>6}")
    return "\n".join(lines)


def errors(entries: list[dict], classify=None) -> list[str]:
    """One line per disagreement, for reading."""
    out = []
    for e in entries:
        gold = {(f, who) for f, who in e["labels"]}
        pred = predict(e, classify)
        for f, who in sorted(pred - gold):
            out.append(f"FP #{e['id']} {f} -> {who}: {e['text'][:140]}")
        for f, who in sorted(gold - pred):
            out.append(f"FN #{e['id']} {f} -> {who}: {e['text'][:140]}")
    return out


def _classifier_from(path: str):
    """``classify`` of another copy of ``news_signals`` (e.g. the previous
    release: ``git show v0.9.0:nfl_mcp/news_signals.py > /tmp/old.py``)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_baseline_news_signals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--split", choices=("dev", "holdout", "holdout2", "holdout3"), help="one split only")
    ap.add_argument("--baseline", help="path of another news_signals.py to score instead")
    ap.add_argument("--errors", action="store_true", help="list each disagreement")
    args = ap.parse_args(argv)
    entries = [e for e in load(args.fixture) if not args.split or e.get("split") == args.split]
    classify = _classifier_from(args.baseline) if args.baseline else None
    print(f"{len(entries)} sentences, {sum(len(e['labels']) for e in entries)} labels")
    print(table(score(entries, classify)))
    if args.errors:
        print("\n".join(errors(entries, classify)))


if __name__ == "__main__":
    main()
