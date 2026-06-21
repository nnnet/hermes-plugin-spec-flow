"""The tree/header node glyph must agree with the node page: a reworked-then-
green node (spec_scope FAIL → later spec_review PASS) is 'reworked' (🔧), NOT
'error' (❌). A genuinely failing node (integrate_verify FAIL, no later pass)
stays 'error'. Regression for the false ❌ on note_search."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _run_dir(tmp_path, events, tree):
    d = tmp_path / "run"
    d.mkdir()
    (d / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")
    (d / "llm-log.jsonl").write_text("", encoding="utf-8")
    return d


def _episodes(state, nid):
    def walk(n):
        if n.get("id") == nid:
            return n.get("episodes", [])
        for c in n.get("children", []):
            r = walk(c)
            if r is not None:
                return r
        return None
    return walk(state["tree"]) or []


def test_reworked_node_is_not_error(tmp_path):
    tree = {"id": "L0", "children": [{"id": "note_search", "children": []}]}
    events = [
        {"tick": 108, "task": "note_search", "phase": "review",
         "gate": "spec_scope", "verdict": "FAIL"},
        {"tick": 113, "task": "note_search", "phase": "review",
         "gate": "spec_review", "verdict": "PASS"},
        {"tick": 124, "task": "note_search", "phase": "lifecycle",
         "action": "to_done"},
    ]
    state = dash._build_state(_run_dir(tmp_path, events, tree))
    eps = _episodes(state, "note_search")
    assert "error" not in eps, f"reworked node must not be error, got {eps}"
    assert "reworked" in eps


def test_genuinely_failing_node_stays_error(tmp_path):
    tree = {"id": "L0", "children": []}
    events = [
        {"tick": 143, "task": "L0", "phase": "integrate",
         "gate": "integrate_verify", "verdict": "FAIL"},
    ]
    state = dash._build_state(_run_dir(tmp_path, events, tree))
    assert "error" in _episodes(state, "L0")
