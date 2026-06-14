"""Axis F wired into the engine: under `isolation: worktree` the leaf
implementer runs against a per-leaf worktree view and its files land in
the shared workspace through a merge-tree-pre-flighted merge. Default
('none') is unchanged — the implementer writes straight into the workspace."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402


def _engine(tmp_path, isolation):
    e = eng.Engine(workspace=str(tmp_path / "wk"))
    e._isolation = isolation
    if isolation == "worktree":
        e.workspace.git_provenance = True
    e.workspace.open()
    return e


def _stub_writer(record):
    def impl(ctx):
        ws = ctx["workspace"]
        fn = ctx["module"]
        record.append((fn, ws.root))
        ws._write(f"src/{fn}.py", f"NAME = '{fn}'\n", "code")
        ws._write(f"tests/test_{fn}.py", f"def test_{fn}(): assert True\n", "test")
    return impl


def test_isolated_leaf_lands_via_merge(tmp_path):
    e = _engine(tmp_path, "worktree")
    seen = []
    e.agents["implementer"] = _stub_writer(seen)
    e._invoke_implementer(
        {"node": "alpha", "title": "Alpha", "module": "alpha",
         "spec": "specs/alpha.md", "workspace": e.workspace}, "alpha", "alpha")
    # the stub ran against an ISOLATED worktree, not the workspace root
    fn, root_used = seen[0]
    assert root_used != e.workspace.root
    # yet the file landed in the shared workspace after the clean merge
    assert (pathlib.Path(e.workspace.root) / "src" / "alpha.py").is_file()


def test_two_isolated_leaves_both_land(tmp_path):
    e = _engine(tmp_path, "worktree")
    seen = []
    e.agents["implementer"] = _stub_writer(seen)
    for leaf in ("alpha", "beta"):
        e._invoke_implementer(
            {"node": leaf, "title": leaf, "module": leaf,
             "spec": f"specs/{leaf}.md", "workspace": e.workspace}, leaf, leaf)
    ws = pathlib.Path(e.workspace.root)
    assert (ws / "src" / "alpha.py").is_file()
    assert (ws / "src" / "beta.py").is_file()


def test_default_mode_writes_into_workspace_directly(tmp_path):
    e = _engine(tmp_path, "none")
    seen = []
    e.agents["implementer"] = _stub_writer(seen)
    e._invoke_implementer(
        {"node": "g", "title": "G", "module": "g",
         "spec": "specs/g.md", "workspace": e.workspace}, "g", "g")
    # no isolation: the stub saw the workspace root itself
    fn, root_used = seen[0]
    assert root_used == e.workspace.root


def test_spec_is_seeded_into_the_worktree(tmp_path):
    e = _engine(tmp_path, "worktree")
    # a spec the leaf will read, written (uncommitted) into the workspace
    spec_rel = "specs/withspec.md"
    sp = pathlib.Path(e.workspace.root) / spec_rel
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("# spec body\n", encoding="utf-8")

    saw = {}

    def impl(ctx):
        ws = ctx["workspace"]
        # the spec must be visible inside the isolated worktree
        saw["spec_present"] = (pathlib.Path(ws.root) / spec_rel).is_file()
        ws._write("src/withspec.py", "X = 1\n", "code")

    e.agents["implementer"] = impl
    e._invoke_implementer(
        {"node": "withspec", "title": "WS", "module": "withspec",
         "spec": spec_rel, "workspace": e.workspace}, "withspec", "withspec")
    assert saw.get("spec_present") is True
