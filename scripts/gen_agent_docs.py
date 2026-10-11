"""Keep AGENT.md / README.md in step with the tool registry.

The tool lists in the docs are hand-written prose, which drifts: counts go
stale, a new tool is never mentioned. This script derives everything countable
from ``nfl_mcp.tool_registry``:

- AGENT.md: a generated tool index (name, profiles, module, one-line
  description) between the ``BEGIN/END GENERATED: tool-index`` markers;
  the total / per-profile counts; and each ``### N. Section (K tools)``
  header's K, counted from the section's ``**`tool`**`` bullets.
- README.md: the badge, the "N MCP tools ... (M in total)" sentence and the
  profile table counts.

It also fails when a registered tool is not mentioned in either document.

Usage:
    python scripts/gen_agent_docs.py           # rewrite the docs in place
    python scripts/gen_agent_docs.py --check   # exit 1 if they are stale (CI)
"""
from __future__ import annotations

import argparse
import inspect
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nfl_mcp import tool_registry

AGENT_MD = ROOT / "AGENT.md"
README_MD = ROOT / "README.md"
BEGIN = "<!-- BEGIN GENERATED: tool-index (scripts/gen_agent_docs.py) -->"
END = "<!-- END GENERATED: tool-index -->"
PROFILE_ORDER = ("season", "offseason", "full")


@dataclass(frozen=True)
class ToolInfo:
    name: str
    module: str
    summary: str
    profiles: tuple[str, ...]


def _all_tools() -> list[Callable]:
    """Every tool, flag-gated ones included (the docs describe the default build)."""
    tools = list(tool_registry._registered_tools())
    names = {t.__name__ for t in tools}
    if "get_league_leaders" not in names:
        from nfl_mcp.tools.nfl_info import get_league_leaders
        tools.append(get_league_leaders)
    return tools


def _profile_members(profile: str, all_names: list[str]) -> set[str]:
    hidden = tool_registry._PROFILE_HIDDEN[profile]
    return {n for n in all_names if n not in hidden}


def _summary(doc: str) -> str:
    """The docstring's first paragraph, cut to its first sentence when long."""
    para = " ".join(line.strip() for line in doc.strip().split("\n\n", 1)[0].splitlines())
    if len(para) > 160:
        m = re.search(r"[.!?](\s|$)", para)
        if m and m.end() <= 240:
            para = para[:m.end()].strip()
    return para


def collect() -> tuple[list[ToolInfo], dict[str, int]]:
    tools = _all_tools()
    names = [t.__name__ for t in tools]
    members = {p: _profile_members(p, names) for p in PROFILE_ORDER}
    infos = []
    for t in tools:
        summary = _summary(inspect.getdoc(t) or "")
        module = (t.__module__ or "").rsplit(".", 1)[-1]
        infos.append(ToolInfo(
            name=t.__name__, module=module, summary=summary,
            profiles=tuple(p for p in PROFILE_ORDER if t.__name__ in members[p]),
        ))
    counts = {p: len(members[p]) for p in PROFILE_ORDER}
    return infos, counts


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_index(infos: list[ToolInfo], counts: dict[str, int]) -> str:
    lines = [
        BEGIN,
        "<!-- Do not edit by hand: run `python scripts/gen_agent_docs.py`. -->",
        "",
        f"All {counts['full']} tools, grouped by the `nfl_mcp/tools/` module that defines them "
        f"(season: {counts['season']}, offseason: {counts['offseason']}, "
        f"full: {counts['full']}).",
        "",
        "| Tool | Profiles | Module | Summary |",
        "|---|---|---|---|",
    ]
    for info in sorted(infos, key=lambda i: (i.module, i.name)):
        lines.append(f"| `{info.name}` | {', '.join(info.profiles)} | `{info.module}` "
                     f"| {_md_cell(info.summary)} |")
    lines.append(END)
    return "\n".join(lines)


def _replace_block(text: str, block: str) -> str:
    start, end = text.find(BEGIN), text.find(END)
    if start == -1 or end == -1:
        raise SystemExit(f"AGENT.md: missing {BEGIN!r} / {END!r} markers")
    return text[:start] + block + text[end + len(END):]


def _sub(pattern: str, repl: str, text: str, label: str, problems: list[str]) -> str:
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n == 0:
        problems.append(f"pattern not found: {label}")
    return new


def _section_counts(text: str) -> str:
    """Set each ``### N. Title (K tools)`` header's K to its bullet count."""
    parts = re.split(r"(?m)^(### \d+\. .*\(\d+ tools?\))$", text)
    out = [parts[0]]
    for header, body in zip(parts[1::2], parts[2::2], strict=True):
        body_until_next = re.split(r"(?m)^## ", body, maxsplit=1)[0]
        tools = set(re.findall(r"(?m)^\s*- \*\*`([a-z_]+)`\*\*", body_until_next))
        k = len(tools)
        header = re.sub(r"\(\d+ tools?\)$", f"({k} tool{'s' if k != 1 else ''})", header)
        out += [header, body]
    return "".join(out)


def render_agent(text: str, infos: list[ToolInfo], counts: dict[str, int],
                 problems: list[str]) -> str:
    text = _replace_block(text, render_index(infos, counts))
    text = _sub(r"The server has \*\*\d+ MCP tools\*\*",
                f"The server has **{counts['full']} MCP tools**", text, "AGENT total", problems)
    for profile, label in (("season", "`season` (default)"), ("offseason", "`offseason`"),
                           ("full", "`full`")):
        text = _sub(rf"^(\| {re.escape(label)} \| )\d+( \|)", rf"\g<1>{counts[profile]}\g<2>",
                    text, f"AGENT profile row {profile}", problems)
    return _section_counts(text)


def render_readme(text: str, counts: dict[str, int], problems: list[str]) -> str:
    s, f = counts["season"], counts["full"]
    text = _sub(r"\[!\[\d+ in-season MCP tools\]\(https://img\.shields\.io/badge/MCP%20tools-"
                r"\d+%20in--season%20%7C%20\d+%20total-",
                f"[![{s} in-season MCP tools](https://img.shields.io/badge/MCP%20tools-"
                f"{s}%20in--season%20%7C%20{f}%20total-", text, "README badge", problems)
    text = _sub(r"^\d+ MCP tools in the default in-season profile \(\d+ in total\)",
                f"{s} MCP tools in the default in-season profile ({f} in total)",
                text, "README sentence", problems)
    for profile, label in (("season", "`season` (default)"), ("offseason", "`offseason`"),
                           ("full", "`full`")):
        text = _sub(rf"^(\| {re.escape(label)} \| )\d+( \|)", rf"\g<1>{counts[profile]}\g<2>",
                    text, f"README profile row {profile}", problems)
    return text


def _unmentioned(text: str, infos: list[ToolInfo], *, outside_block: bool) -> list[str]:
    if outside_block and BEGIN in text:
        text = text[:text.find(BEGIN)] + text[text.find(END) + len(END):]
    return [i.name for i in infos if f"`{i.name}`" not in text]


def run(check: bool) -> int:
    infos, counts = collect()
    problems: list[str] = []
    agent_old, readme_old = AGENT_MD.read_text(), README_MD.read_text()
    agent_new = render_agent(agent_old, infos, counts, problems)
    readme_new = render_readme(readme_old, counts, problems)

    missing_agent = _unmentioned(agent_new, infos, outside_block=True)
    missing_readme = _unmentioned(readme_new, infos, outside_block=False)
    if missing_agent:
        problems.append(f"AGENT.md never describes: {', '.join(missing_agent)}")
    if missing_readme:
        problems.append(f"README.md never mentions: {', '.join(missing_readme)}")

    stale = [p.name for p, old, new in ((AGENT_MD, agent_old, agent_new),
                                        (README_MD, readme_old, readme_new)) if old != new]
    if check:
        if stale:
            problems.append(f"stale: {', '.join(stale)} (run `python scripts/gen_agent_docs.py`)")
    else:
        AGENT_MD.write_text(agent_new)
        README_MD.write_text(readme_new)
        for name in stale:
            print(f"updated {name}")
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the docs are stale")
    return run(check=ap.parse_args().check)


if __name__ == "__main__":
    sys.exit(main())
