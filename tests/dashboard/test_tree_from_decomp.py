"""The dashboard rebuilds the task tree from the engine's decomposition journal
(.spec-flow/decomp.jsonl). This is authoritative on a RESUME: restored nodes
never re-ask the decomposer, so they leave no llm-log outcome — rebuilding from
the llm-log alone then loses the real root and a late node floats to the top
(L0 ended up UNDER product_entry). From decomp.jsonl the root stays L0."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
import live_dashboard as ld   # noqa: E402


def _write_decomp(run_dir, rows):
    d = run_dir / "workspace" / ".spec-flow"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "decomp.jsonl").open("w", encoding="utf-8") as fh:
        for node, children in rows:
            fh.write(json.dumps(
                {"node": node,
                 "payload": {"children": [{"id": c, "title": c} for c in children]}})
                + "\n")


def test_root_is_L0_with_restored_children(tmp_path):
    # only L0 + its leaves are journaled (a resume where the decomposer was not
    # re-run); the tree must root at L0, not at a leaf
    _write_decomp(tmp_path, [("L0", ["notes_database", "notes_api_layer"]),
                             ("notes_database", []), ("notes_api_layer", [])])
    tree = ld._tree_from_decomp(tmp_path)
    assert tree["id"] == "L0"
    assert {c["id"] for c in tree["children"]} == {"notes_database", "notes_api_layer"}


def test_late_root_level_node_does_not_become_the_root(tmp_path):
    # product_entry is a late root-level node (no children, nobody's child) — it
    # must NOT swallow L0; L0 stays the root and product_entry is left for the
    # orphan-attach step to hang under it
    _write_decomp(tmp_path, [("L0", ["notes_database"]),
                             ("notes_database", []),
                             ("product_entry", [])])
    tree = ld._tree_from_decomp(tmp_path)
    assert tree["id"] == "L0"
    assert "product_entry" not in {c["id"] for c in tree["children"]}


def test_absent_journal_returns_none(tmp_path):
    assert ld._tree_from_decomp(tmp_path) is None


def test_cycle_in_journal_is_guarded(tmp_path):
    # never trust the input: a self/loop reference must not recurse forever
    _write_decomp(tmp_path, [("L0", ["a"]), ("a", ["L0"])])
    tree = ld._tree_from_decomp(tmp_path)
    assert tree["id"] == "L0"   # builds without RecursionError
