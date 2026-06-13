"""Axis F (physical isolation): each leaf builds in its own git worktree
on its own branch; re-integration is a serialized, merge-tree-pre-flighted
merge. Disjoint leaves merge cleanly; a real overlap is refused, not forced."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ws_tx  # noqa: E402

_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": "/tmp/claude", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _read(root, rel):
    return (pathlib.Path(root) / rel).read_text(encoding="utf-8")


def _head_files(root):
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                         cwd=root, env=_ENV, capture_output=True, text=True)
    return set(out.stdout.split())


# ─── merge-tree pre-flight in isolation ───────────────────────────────

def test_preflight_clean_for_disjoint_branches(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    with ws_tx.leaf_worktree(root, "alpha", "leaf:alpha") as wa:
        (pathlib.Path(wa.path) / "src").mkdir(parents=True, exist_ok=True)
        (pathlib.Path(wa.path) / "src" / "alpha.py").write_text("A = 1\n")
    assert wa.merged and not wa.conflicts
    assert "src/alpha.py" in _head_files(root)


def test_two_disjoint_leaves_both_land(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    for leaf in ("alpha", "beta"):
        with ws_tx.leaf_worktree(root, leaf, f"leaf:{leaf}") as wt:
            d = pathlib.Path(wt.path) / "src"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{leaf}.py").write_text(f"NAME = '{leaf}'\n")
        assert wt.merged
    files = _head_files(root)
    assert {"src/alpha.py", "src/beta.py"} <= files


def test_conflicting_leaf_is_refused_not_forced(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    # seed a shared file on the workspace baseline
    shared = pathlib.Path(root) / "shared.py"
    shared.write_text("V = 0\n")
    subprocess.run(["git", "add", "-A"], cwd=root, env=_ENV)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "seed"], cwd=root, env=_ENV)

    # leaf 1 rewrites shared.py and lands
    with ws_tx.leaf_worktree(root, "one", "leaf:one") as w1:
        (pathlib.Path(w1.path) / "shared.py").write_text("V = 1\n")
    assert w1.merged
    # leaf 2 was branched off the SAME baseline and also rewrites shared.py;
    # its pre-flight against the NEW head must see the overlap and refuse
    wt2 = ws_tx.leaf_worktree(root, "two", "leaf:two")
    wt2.__enter__()
    # force the worktree onto the OLD baseline so the edit genuinely diverges
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=wt2.path,
                   env=_ENV, capture_output=True)
    (pathlib.Path(wt2.path) / "shared.py").write_text("V = 2\n")
    wt2.__exit__(None, None, None)
    assert not wt2.merged and wt2.conflicts == ["shared.py"]
    # the workspace still holds leaf 1's value — the bad merge never landed
    assert _read(root, "shared.py") == "V = 1\n"


def test_worktree_and_branch_are_cleaned_up(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    with ws_tx.leaf_worktree(root, "gamma", "leaf:gamma") as wt:
        wt_path = wt.path
        (pathlib.Path(wt.path) / "g.py").write_text("g = 1\n")
    # the temp worktree dir is gone and the leaf branch is deleted
    assert not pathlib.Path(wt_path).exists()
    br = subprocess.run(["git", "branch", "--list", wt.branch],
                        cwd=root, env=_ENV, capture_output=True, text=True)
    assert br.stdout.strip() == ""


def test_no_git_degrades_to_workspace_writes(tmp_path, monkeypatch):
    # when the workspace can't become a repo, the leaf still writes (degraded)
    monkeypatch.setattr(ws_tx, "ensure_repo", lambda root: False)
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    with ws_tx.leaf_worktree(root, "d", "leaf:d") as wt:
        assert wt.path == root          # writes go straight to the workspace
        (pathlib.Path(wt.path) / "d.py").write_text("d = 1\n")
    assert (pathlib.Path(root) / "d.py").is_file()
