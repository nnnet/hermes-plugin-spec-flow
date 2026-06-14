"""Git-backed workspace transactions: isolation (queue), atomicity
(commit/rollback incl. created files), attribution (author per commit).
The old hand-rolled snapshot restored only OVERWRITTEN files — a repair
that CREATED a poisoning file left it behind after 'rollback'."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ws_tx  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, env=ws_tx._GIT_ENV,
                          capture_output=True, text=True)


def _mk(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n", encoding="utf-8")
    assert ws_tx.ensure_repo(str(tmp_path))
    return tmp_path


def test_commit_records_author_and_content(tmp_path):
    root = _mk(tmp_path)
    with ws_tx.transaction(str(root), "leaf:cart", "write+bar"):
        (root / "src/cart.py").write_text("def add(): pass\n",
                                          encoding="utf-8")
    log = _git(root, "log", "--format=%an|%s", "-1").stdout
    assert "leaf:cart" in log and "write+bar" in log
    # the file is durably in history
    show = _git(root, "show", "HEAD:src/cart.py").stdout
    assert "def add" in show


def test_rollback_restores_modified_and_removes_created(tmp_path):
    root = _mk(tmp_path)
    with ws_tx.transaction(str(root), "verifier:beta", "repair") as tx:
        (root / "src/app.py").write_text("x = 666\n", encoding="utf-8")
        (root / "src/poison.py").write_text("boom\n", encoding="utf-8")
        tx.rollback()
    assert (root / "src/app.py").read_text() == "x = 1\n"
    assert not (root / "src/poison.py").exists(), \
        "a CREATED file must vanish on rollback (old snapshot missed this)"


def test_exception_auto_rolls_back(tmp_path):
    root = _mk(tmp_path)
    try:
        with ws_tx.transaction(str(root), "leaf:x", "write"):
            (root / "src/app.py").write_text("half-written", encoding="utf-8")
            raise RuntimeError("crash mid-write")
    except RuntimeError:
        pass
    assert (root / "src/app.py").read_text() == "x = 1\n", \
        "an exception mid-transaction must not leave a half-commit"


def test_empty_transaction_makes_no_commit(tmp_path):
    root = _mk(tmp_path)
    head0 = _git(root, "rev-parse", "HEAD").stdout
    with ws_tx.transaction(str(root), "leaf:noop", "nothing"):
        pass
    assert _git(root, "rev-parse", "HEAD").stdout == head0


def test_attribution_survives_many_writers(tmp_path):
    root = _mk(tmp_path)
    for who, fname in [("leaf:a", "a.py"), ("leaf:b", "b.py"),
                       ("verifier:L0", "a.py")]:
        with ws_tx.transaction(str(root), who, "write"):
            (root / "src" / fname).write_text(f"# by {who}\n",
                                              encoding="utf-8")
    blame = _git(root, "log", "--format=%an", "--", "src/a.py").stdout.split()
    assert blame == ["verifier:L0", "leaf:a"], \
        "git log must answer 'who wrote this file' exactly"


def test_ensure_repo_idempotent(tmp_path):
    root = _mk(tmp_path)
    assert ws_tx.ensure_repo(str(root))   # second call: already a repo
