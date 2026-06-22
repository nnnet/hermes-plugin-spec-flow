"""Process tiering: a simple single-concern leaf builds with ONE coder pass
(TDD generate + one repair) instead of the four-call architect->coder->tester->
fixer orchestra. The orchestra is reserved for branches and large/undecided
leaves. The decision is purely structural (the node's complexity label), never
the model — offline, deterministic, model-independent."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import spec_flow_remedies as rem      # noqa: E402
from spec_flow_runner import Engine    # noqa: E402


def _runner(meta=None):
    r = Engine.__new__(Engine)
    r._project_meta = meta or {}
    r.review_policy = {"simple_max_loc": 60}
    return r


# --- accessor: workers.solo_for_labels, env, default --------------------------

def test_default_solo_labels_is_leaf_small():
    assert rem.solo_for_labels({}) == ["leaf_small"]


def test_solo_labels_from_project_meta():
    meta = {"workers": {"solo_for_labels": ["leaf_small", "leaf_big"]}}
    assert rem.solo_for_labels(meta) == ["leaf_small", "leaf_big"]


def test_solo_labels_empty_list_disables_tiering():
    # an explicit empty list restores always-orchestra (no leaf goes solo)
    assert rem.solo_for_labels({"workers": {"solo_for_labels": []}}) == []


def test_solo_labels_env_override(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_SOLO_LABELS", '["branch"]')
    assert rem.solo_for_labels({"workers": {"solo_for_labels": ["leaf_small"]}}) \
        == ["branch"]


def test_solo_labels_bad_env_ignored(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_SOLO_LABELS", "not json")
    assert rem.solo_for_labels({}) == ["leaf_small"]


# --- engine decision: _node_solo ----------------------------------------------

def test_small_leaf_is_solo():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 30, "open_decisions": 0}}
    assert r._node_solo(node) is True


def test_big_leaf_is_not_solo():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 200, "open_decisions": 0}}
    assert r._node_solo(node) is False


def test_open_decisions_block_solo():
    r = _runner()
    node = {"id": "n", "metrics": {"estimated_loc": 20, "open_decisions": 1}}
    assert r._node_solo(node) is False


def test_branch_never_solo():
    r = _runner()
    node = {"id": "n", "children": [{"id": "c"}], "metrics": {}}
    assert r._node_solo(node) is False


def test_product_entry_never_solo():
    # the assembly entry wires every module — never a one-shot solo
    r = _runner()
    node = {"id": "product_entry", "metrics": {"estimated_loc": 20,
                                               "open_decisions": 0}}
    assert r._node_solo(node) is False


def test_empty_label_set_disables_solo():
    r = _runner({"workers": {"solo_for_labels": []}})
    node = {"id": "n", "metrics": {"estimated_loc": 10, "open_decisions": 0}}
    assert r._node_solo(node) is False


def test_label_set_can_widen_to_big():
    r = _runner({"workers": {"solo_for_labels": ["leaf_small", "leaf_big"]}})
    node = {"id": "n", "metrics": {"estimated_loc": 300, "open_decisions": 0}}
    assert r._node_solo(node) is True


# --- complexity label helper (shared by tier + solo) --------------------------

def test_complexity_label_values():
    r = _runner()
    assert r._node_complexity_label(
        {"metrics": {"estimated_loc": 10, "open_decisions": 0}}) == "leaf_small"
    assert r._node_complexity_label(
        {"metrics": {"estimated_loc": 999, "open_decisions": 0}}) == "leaf_big"
    assert r._node_complexity_label(
        {"children": [{}], "metrics": {}}) == "branch"
