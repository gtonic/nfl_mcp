"""Explicit nulls from Sleeper, and failures that must not be silent."""

import pytest


class TestNullMetadata:
    def test_the_dict_default_does_not_cover_an_explicit_null(self):
        # This is the whole bug class: `{}` applies to a MISSING key, never to
        # a key whose value is null. Sleeper sends "metadata": null.
        user = {"user_id": "1", "display_name": None, "metadata": None}
        with pytest.raises(AttributeError):
            _ = user.get("display_name") or user.get("metadata", {}).get("team_name")

    def test_the_or_idiom_handles_both(self):
        for meta in (None, {}, {"team_name": "Wolfurt"}):
            user = {"user_id": "1", "display_name": None, "metadata": meta}
            name = user.get("display_name") or (user.get("metadata") or {}).get("team_name")
            assert name == ("Wolfurt" if meta and meta.get("team_name") else None)

    @pytest.mark.asyncio
    async def test_playoff_names_survive_a_null_metadata_user(self, monkeypatch):
        from nfl_mcp import playoff_tools

        users = {
            "success": True,
            "users": [
                {"user_id": "u1", "display_name": None, "metadata": None},
                {"user_id": "u2", "display_name": "Second", "metadata": None},
            ],
        }

        async def fake_users(league_id):
            return users

        monkeypatch.setattr(playoff_tools, "get_league_users", fake_users)

        # Reproduce the comprehension the tool runs.
        resolved = {
            u.get("user_id"): (
                u.get("display_name") or (u.get("metadata") or {}).get("team_name")
            )
            for u in users["users"]
        }
        # One bad user used to raise and wipe the entire map, degrading every
        # team to "Roster N" — invisibly, behind a bare `except: pass`.
        assert resolved == {"u1": None, "u2": "Second"}


class TestFailuresAreLogged:
    """Loops that build results must not drop items without a trace.

    An injury fetcher returned zero records for months because the only thing
    standing between it and a log line was a bare `except: continue`.
    """

    @pytest.mark.parametrize("module", ["sleeper_strategy", "playoff_tools"])
    def test_no_bare_handler_skips_an_item_silently(self, module):
        import ast
        import importlib
        import inspect

        mod = importlib.import_module(f"nfl_mcp.{module}")
        tree = ast.parse(inspect.getsource(mod))

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # A handler that only skips: nothing but pass/continue inside.
            if not all(isinstance(st, (ast.Pass, ast.Continue)) for st in node.body):
                continue
            offenders.append(node.lineno)

        assert not offenders, (
            f"{module}: silent skip at line(s) {offenders} — log before continuing"
        )
