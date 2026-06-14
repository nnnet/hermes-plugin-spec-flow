"""#7: spec-hash-aware resume. The journal stamps each leaf's spec hash; a
resume reuses a node whose spec is UNCHANGED and re-runs one whose spec
CHANGED. Builds on П1 resume + П2 journal."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402


def _ws(tmp_path):
    from spec_flow_runner import Workspace
    ws = Workspace(root=str(tmp_path / "wk"), enabled=True)
    ws.open()
    return ws


# ─── journal records + reads spec hash ────────────────────────────────

def test_journal_mark_records_spec_hash(tmp_path):
    ws = _ws(tmp_path)
    (pathlib.Path(ws.root) / "specs").mkdir(parents=True, exist_ok=True)
    (pathlib.Path(ws.root) / "specs" / "cart.md").write_text("# cart\n")
    h = ws.spec_hash("specs/cart.md")
    assert h and len(h) == 16
    ws.journal_mark("cart", 1, h)
    idx = ws.journal_index()
    assert idx["cart"]["spec_hash"] == h
    assert ws.journal_nodes() == {"cart"}


def test_spec_hash_changes_with_content(tmp_path):
    ws = _ws(tmp_path)
    specs = pathlib.Path(ws.root) / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "a.md").write_text("one\n")
    h1 = ws.spec_hash("specs/a.md")
    (specs / "a.md").write_text("two\n")
    h2 = ws.spec_hash("specs/a.md")
    assert h1 != h2


def test_spec_hash_missing_is_empty(tmp_path):
    ws = _ws(tmp_path)
    assert ws.spec_hash("specs/ghost.md") == ""


def test_journal_index_later_record_wins(tmp_path):
    ws = _ws(tmp_path)
    ws.journal_mark("n", 1, "aaaa")
    ws.journal_mark("n", 2, "bbbb")
    assert ws.journal_index()["n"] == {"version": 2, "spec_hash": "bbbb"}


# ─── end-to-end: resume skips unchanged, re-runs changed ──────────────

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 1,
           "human_in_loop": False, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


def _make_decomp(cart_spec_text):
    # the engine REGENERATES specs/<fn>.md each run from the decomposer's
    # spec_markdown; a different spec_markdown → different spec hash → cache
    # miss on resume. cart's text is parameterized; pay's is constant.
    def _decomp(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BRANCH), "children": [
                {"id": "cart_feature", "title": "Cart feature"},
                {"id": "pay_feature", "title": "Pay feature"}]}
        title = ctx["node"].get("title", "")
        if "Cart" in title:
            return {"metrics": dict(_LEAF),
                    "spec_markdown": f"## Requirements\n- {cart_spec_text}"}
        return {"metrics": dict(_LEAF),
                "spec_markdown": "## Requirements\n- pay stays constant"}
    return _decomp


def _run(root, resume, impl_log, cart_spec_text):
    def impl(ctx):
        impl_log.append(ctx["node"])
        ws = ctx["workspace"]
        fn = ctx["module"]
        ws._write(f"src/{fn}.py", f"X = '{fn}'\n", "code")
        ws._write(f"tests/test_{fn}.py", "def test_x():\n    assert True\n", "test")
    e = eng.Engine(workspace=root, depth="execute", resume=resume,
                   agents={"decomposer": _make_decomp(cart_spec_text),
                           "implementer": impl})
    e.run({"name": "c", "goal": "g", "target": "x", "policy": _POLICY})
    return e


def test_resume_reuses_unchanged_reruns_changed(tmp_path):
    root = str(tmp_path / "wk")
    first = []
    _run(root, False, first, "cart v1")
    assert set(first) >= {"cart_feature", "pay_feature"}

    # resume with a CHANGED cart spec (different decomposer output) but the
    # SAME pay spec → cart's regenerated hash differs, pay's matches
    second = []
    _run(root, True, second, "cart v2 CHANGED")
    assert "cart_feature" in second        # spec changed → re-run
    assert "pay_feature" not in second      # spec unchanged → reused


def test_resume_reuses_all_when_specs_identical(tmp_path):
    root = str(tmp_path / "wk")
    first = []
    _run(root, False, first, "cart v1")
    second = []
    _run(root, True, second, "cart v1")    # identical decomposer output
    # nothing changed → both reused, neither re-implemented
    assert second == []
