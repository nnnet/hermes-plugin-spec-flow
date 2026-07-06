"""Audit: the hand-rolled SEARCH/REPLACE applier in tests/harness/diff_repair.py
must carry a RECORDED REASON for not using a diff library — so the NIH can
never silently return (node H1b).

Why: diff_repair.py hand-rolls an aider-style SEARCH/REPLACE patch applier.
A generic diff library (diff-match-patch / unidiff / python-patch) nominally
covers "apply a patch to text", so a reader is owed the deliberate reason the
wheel was re-cut here — exactly as the wave scheduler (H1) and the closed-world
IR (H7) record theirs. This is the ONLY hand-rolled harness component that
lacked such a note; every other one documents its justification. This audit
pins the note present: it must name at least one candidate library AND the
domain behaviours the library does not give (the FORMAT contract, empty-SEARCH
= whole-file replace, exact-once refusal, the write-door integration).

What: parses the module docstring of tests/harness/diff_repair.py and asserts
the recorded-reason markers are there. It checks for MARKERS (a candidate
library name + reason keywords), never one exact sentence — the reason may be
reworded, but it may never vanish.

Test: run this file; RED when the note is absent, GREEN once the docstring
records the library + reason. `.venv/bin/python -m pytest tests/audit -q`.
"""
from __future__ import annotations

import ast
import pathlib

_DIFF_REPAIR = (
    pathlib.Path(__file__).resolve().parents[1] / "harness" / "diff_repair.py"
)

# A generic diff/patch library that nominally covers "apply a patch to text".
# At least one must be NAMED as the thing we deliberately did not use.
_CANDIDATE_LIBS = ("diff-match-patch", "diff_match_patch",
                   "unidiff", "python-patch", "python patch")

# Domain behaviours a generic diff library does not provide — the note must
# ground the decision in at least a few of these (the actual reasons, from the
# code): the model-facing block FORMAT contract, empty-SEARCH = whole-file,
# refusal on an ambiguous / multiple / missing match, the write-door tie-in.
_REASON_MARKERS = ("format contract", "empty search", "empty-search",
                   "whole-file", "whole file", "ambiguous", "exactly once",
                   "exact-once", "refus", "write door", "write-door")


def _module_docstring() -> str:
    """Read diff_repair.py's module docstring WITHOUT importing it (path-based,
    so the audit stays free of the harness import chain).
    Test: returns a non-empty str for the real file."""
    src = _DIFF_REPAIR.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src)) or ""
    return doc.lower()


def test_diff_repair_module_exists():
    """Why: the audit is meaningless if the target moved.
    What: the hand-rolled applier file is where we expect it.
    Test: asserts the path exists."""
    assert _DIFF_REPAIR.is_file(), f"missing {_DIFF_REPAIR}"


def test_diff_repair_names_a_candidate_library():
    """Why: a silent NIH hides which wheel was re-cut; naming the library the
    reader would reach for makes the decision auditable.
    What: the module docstring names at least one generic diff/patch library.
    Test: RED when no candidate library is named, GREEN once one is."""
    doc = _module_docstring()
    hit = [lib for lib in _CANDIDATE_LIBS if lib in doc]
    assert hit, (
        "diff_repair.py hand-rolls a SEARCH/REPLACE applier but its docstring "
        "names no candidate diff library (one of "
        f"{_CANDIDATE_LIBS}); the recorded reason for the NIH is missing."
    )


def test_diff_repair_records_the_domain_reason():
    """Why: naming a library without saying WHY it does not fit is only half
    the recorded reason — the domain behaviours are the justification.
    What: the docstring grounds the hand-roll in the specific behaviours a
    generic diff library does not give (block FORMAT contract, empty-SEARCH =
    whole-file, refusal on an ambiguous / missing match, write-door tie-in).
    Test: RED with < 3 domain markers present, GREEN once the reason is
    written; markers (not one sentence), so the wording may change."""
    doc = _module_docstring()
    present = [m for m in _REASON_MARKERS if m in doc]
    assert len(present) >= 3, (
        "diff_repair.py docstring must record WHY a diff library is not used — "
        "the domain behaviours it does not provide (block FORMAT contract, "
        "empty-SEARCH = whole-file, ambiguous/missing-match refusal, "
        "write-door tie-in). "
        f"found only {present} of {_REASON_MARKERS}."
    )
