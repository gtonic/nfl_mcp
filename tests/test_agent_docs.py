"""AGENT.md / README.md must match the tool registry (scripts/gen_agent_docs.py)."""
from scripts import gen_agent_docs


def test_agent_and_readme_docs_are_current(capsys):
    rc = gen_agent_docs.run(check=True)
    err = capsys.readouterr().err
    assert rc == 0, f"{err}\nRegenerate with: python scripts/gen_agent_docs.py"


def test_index_lists_every_tool_once():
    infos, counts = gen_agent_docs.collect()
    block = gen_agent_docs.render_index(infos, counts)
    for info in infos:
        assert block.count(f"| `{info.name}` |") == 1
    assert counts["full"] == len(infos)
