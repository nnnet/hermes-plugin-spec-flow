"""Lever #1: late requirements run in parallel WAVES. Requirements that EDIT
THE SAME owner module (same code_target) must serialize (write conflict);
requirements targeting DIFFERENT modules, or each creating a NEW module, share
a wave (parallel under worktree isolation). depends_on is honoured. Pure +
offline — the grouping is decided purely by code_target + deps, never the
model."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine    # noqa: E402


def _r():
    return Engine.__new__(Engine)


def _ids(waves):
    return [[p["id"] for p in w] for w in waves]


def test_distinct_targets_one_wave():
    r = _r()
    placements = [
        {"id": "a", "code_target": "src/x.py"},
        {"id": "b", "code_target": "src/y.py"},
    ]
    assert _ids(r._late_req_waves(placements)) == [["a", "b"]]


def test_same_target_serializes():
    r = _r()
    placements = [
        {"id": "a", "code_target": "src/web_ui.py"},
        {"id": "b", "code_target": "src/web_ui.py"},
    ]
    assert _ids(r._late_req_waves(placements)) == [["a"], ["b"]]


def test_new_modules_no_target_one_wave():
    r = _r()
    placements = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert _ids(r._late_req_waves(placements)) == [["a", "b", "c"]]


def test_mixed_same_and_distinct():
    r = _r()
    placements = [
        {"id": "a", "code_target": "src/web_ui.py"},
        {"id": "b", "code_target": "src/notes.py"},
        {"id": "c", "code_target": "src/web_ui.py"},
    ]
    # a+b parallel; c (same target as a) falls to the next wave
    assert _ids(r._late_req_waves(placements)) == [["a", "b"], ["c"]]


def test_depends_on_waits():
    r = _r()
    placements = [
        {"id": "a"},
        {"id": "b", "depends_on": ["a"]},
    ]
    assert _ids(r._late_req_waves(placements)) == [["a"], ["b"]]


def test_external_depends_on_ignored():
    r = _r()
    # a dep that is NOT among the placements (already-built node) is not a blocker
    placements = [{"id": "b", "depends_on": ["already_done"]}]
    assert _ids(r._late_req_waves(placements)) == [["b"]]


def test_self_dep_does_not_block():
    r = _r()
    placements = [{"id": "a", "depends_on": ["a"]}]
    assert _ids(r._late_req_waves(placements)) == [["a"]]


def test_cycle_degrades_to_one_wave():
    r = _r()
    placements = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ]
    # neither can lead — rather than deadlock, the blocked remainder runs together
    assert _ids(r._late_req_waves(placements)) == [["a", "b"]]


def test_empty():
    assert _r()._late_req_waves([]) == []
