"""Offline unit tests: the parser/verifier, the LLM-fallback seam, and the width guard.

No network, no secrets, no host-specific paths — everything here runs on a clean checkout:
    ./venv/bin/python -m pytest -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))

import crochet_chart as cc          # noqa: E402
import render_chart as rc           # noqa: E402
import pattern_to_chart as p2c      # noqa: E402


def test_shipped_sample_pattern_verifies_completely():
    verified, unverified = p2c.convert(os.path.join(ROOT, "sample", "pattern.pdf"),
                                       verbose=False, out_json=os.devnull, use_llm=False)
    assert unverified == {}
    assert sorted(verified) == list(range(1, 11))


def test_a_row_that_does_not_reconcile_is_refused():
    items, count, notes = cc.parse_row("SC in 1st ST, DC in next ST (999)", count_hint=5)
    assert items is None
    assert notes                       # a reason is always given


def test_learned_clause_is_accepted_only_if_it_reconciles(monkeypatch):
    # Simulate the LLM fallback registering a reading for a clause the rules do not know, then
    # prove the learned reading flows through the SAME verifier: accepted when it reconciles,
    # refused when it does not.
    monkeypatch.setitem(cc.LEARNED, "DOODLE in next ST", ("DC", 1))
    ok, _count, _notes = cc.parse_row("SC in 1st ST, DOODLE in next ST (2)")
    assert ok is not None and sum(s for _, s in ok) == 2
    bad, _count, notes = cc.parse_row("SC in 1st ST, DOODLE in next ST (7)")
    assert bad is None and notes


def test_width_guard_refuses_an_absurd_chart():
    old = rc.rows
    try:
        rc.rows = {1: [("SC", 1)] * 5000}
        with pytest.raises(ValueError, match="too wide"):
            rc.render([1], path=os.path.join(rc.__dict__.get("TMPDIR", "/tmp"), "never.png"),
                      legend=False)
    finally:
        rc.rows = old
