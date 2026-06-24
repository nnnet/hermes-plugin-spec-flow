"""Proof that the SOLO build path lands its source in the main workspace —
there is NO solo-specific merge-back bug. The write->commit->merge sequence is
shared by solo and orchestra: both feed the model's files through
_write_reply_files into the leaf worktree, which commits everything (git add -A)
and merges it back unfiltered (git merge --no-ff). When valid model output is
present, src/<fn>.py lands. The 'only app.py' symptom in live runs was missing
MODEL output (degraded weak models) + the now-fixed paid-guard/escalation gap,
NOT a merge defect.

Hermetic: no HTTP, no LLM — exercises the real _write_reply_files + the real
ws_tx.leaf_worktree merge with a controlled files dict (what a working model
would return)."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import ws_tx                # noqa: E402
from harness import role_worker as rw    # noqa: E402

_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": "/tmp/claude", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _head_files(root):
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                         cwd=root, env=_ENV, capture_output=True, text=True)
    return set(out.stdout.split())


class _View:
    """Minimal workspace view rooted at the leaf worktree — mirrors what the
    engine's _LeafWorkspaceView gives the implementer: _write lands at root."""
    def __init__(self, root):
        self.root = root

    def _write(self, rel, content, kind):
        p = pathlib.Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return rel


def test_solo_files_land_in_main_via_worktree(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    # what a WORKING coder model returns for node 'notes' (solo single pass)
    files = {"src/notes.py": "def value():\n    return 1\n",
             "tests/test_notes.py": "def test_value():\n    assert True\n"}
    with ws_tx.leaf_worktree(root, "notes", "leaf:notes") as wt:
        wrote = rw._write_reply_files(_View(wt.path), files, "notes")
    assert wrote is True
    assert wt.merged and not wt.conflicts
    # the SOURCE landed in the shared workspace — not just a spec
    landed = _head_files(root)
    assert "src/notes.py" in landed
    assert "tests/test_notes.py" in landed


def test_empty_model_output_lands_nothing(tmp_path):
    # the actual live failure mode: the model returned no usable files, so
    # _write_reply_files writes nothing and only whatever was seeded persists —
    # this is missing MODEL output, reproduced here, NOT a merge defect
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    with ws_tx.leaf_worktree(root, "notes", "leaf:notes") as wt:
        wrote = rw._write_reply_files(_View(wt.path), {}, "notes")
    assert wrote is False
    assert "src/notes.py" not in _head_files(root)


def test_misnamed_output_key_is_dropped(tmp_path):
    # a real robustness gap (shared by solo+orchestra, not solo-specific):
    # _write_reply_files only accepts EXACTLY src/<fn>.py — if the model names
    # the file differently it is silently dropped
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    ws_tx.ensure_repo(root)
    with ws_tx.leaf_worktree(root, "notes_database", "leaf:nd") as wt:
        wrote = rw._write_reply_files(
            _View(wt.path), {"src/notes.py": "X = 1\n"}, "notes_database")
    assert wrote is False           # key src/notes.py != expected src/notes_database.py
    assert "src/notes.py" not in _head_files(root)
