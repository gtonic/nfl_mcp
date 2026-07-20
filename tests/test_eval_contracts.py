"""Offline tests for the contract-check runner (no network).

The checks themselves are live (Layer B, scheduled). Here we only verify the
runner's isolation and exit-code semantics with fake checks.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.contracts import checks as C


def _fake(passing_critical=True, failing_warn=True):
    reg = []

    def ok():
        return "fine"

    def boom():
        raise ValueError("upstream changed")

    if passing_critical:
        reg.append(("ok.crit", True, ok))
    if failing_warn:
        reg.append(("bad.warn", False, boom))
    return reg


def test_run_all_isolates_failures(monkeypatch):
    monkeypatch.setattr(C, "CHECKS", _fake())
    results = C.run_all()
    by = {r["name"]: r for r in results}
    assert by["ok.crit"]["ok"] is True
    assert by["bad.warn"]["ok"] is False
    assert "upstream changed" in by["bad.warn"]["detail"]


def test_exit_code_warn_only_is_zero():
    results = [
        {"name": "a", "critical": True, "ok": True, "detail": ""},
        {"name": "b", "critical": False, "ok": False, "detail": "x"},  # warn fail
    ]
    assert C.exit_code(results) == 0


def test_exit_code_critical_fail_is_one():
    results = [
        {"name": "a", "critical": True, "ok": False, "detail": "x"},  # crit fail
        {"name": "b", "critical": False, "ok": True, "detail": ""},
    ]
    assert C.exit_code(results) == 1


def test_all_registered_checks_have_names():
    # Sanity: the real registry is populated and well-formed.
    assert len(C.CHECKS) >= 5
    for name, critical, fn in C.CHECKS:
        assert isinstance(name, str) and callable(fn) and isinstance(critical, bool)
