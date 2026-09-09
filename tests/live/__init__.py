"""Live regression harness: one real Claude Code session, one real daemon,
the real API, and assertions on the numbers that cost users money.

Everything in `tests/` outside this package runs against the fake upstream
in `tests/conftest.py`. This package is the deliberate exception: it exists
because a bump that silently starts paying `cache_creation` instead of
reading the cache passes every offline test. It is never collected by
`pytest` (no module here is named `test_*`); `make live` runs it, and
`tests/test_live_harness.py` covers its own logic offline.

See `docs/DESIGN.md`, "Live regression harness", for why it is shaped this
way -- in particular why assertions read the control API and the subject's
transcript rather than the terminal.
"""
