"""Live integration tools you run by hand against real Sleeper data.

Unlike the unit tests (mocked) and the scheduled evals, these drive the real code
paths against a real league/draft:

- ``validate_draft`` — a pre-flight check that the whole draft flow works.
- ``draft_watch`` — a live "war room" watcher that recommends a pick each time
  you're on the clock.

See ``docs/DRAFT_DAY.md`` for the full draft-day playbook.
"""
