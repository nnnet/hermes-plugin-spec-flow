"""Optional granular commits (#1 follow-up): when enabled, a leaf commits
after CREATE and after each REPAIR, not only once at close — a fine-grained
git history of how the leaf converged. Default OFF; gated by env or config."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import role_worker as rw  # noqa: E402
from harness import llm_backend as lb  # noqa: E402

_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": "/tmp/claude", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _commits(root):
    out = subprocess.run(["git", "log", "--oneline"], cwd=root, env=_ENV,
                         capture_output=True, text=True)
    return out.stdout.strip().splitlines()


# ─── the toggle ───────────────────────────────────────────────────────

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_GRANULAR_COMMITS", raising=False)
    lb.configure_workers(None)
    assert rw._granular_commits() is False


def test_enabled_by_env(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_GRANULAR_COMMITS", "1")
    assert rw._granular_commits() is True
    monkeypatch.setenv("SPEC_FLOW_GRANULAR_COMMITS", "off")
    assert rw._granular_commits() is False


def test_enabled_by_config(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_GRANULAR_COMMITS", raising=False)
    lb.configure_workers({"granular_commits": True})
    assert rw._granular_commits() is True
    lb.configure_workers(None)


# ─── the commit action ────────────────────────────────────────────────

def test_commit_when_on_writes_a_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_GRANULAR_COMMITS", "1")
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    from harness import ws_tx
    ws_tx.ensure_repo(root)
    (pathlib.Path(root) / "src").mkdir()
    (pathlib.Path(root) / "src" / "cart.py").write_text("x = 1\n")
    before = len(_commits(root))
    rw._granular_commit(root, "cart", "create")
    after = _commits(root)
    assert len(after) == before + 1
    assert after[0].endswith("create: cart")


def test_no_commit_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_GRANULAR_COMMITS", raising=False)
    lb.configure_workers(None)
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    from harness import ws_tx
    ws_tx.ensure_repo(root)
    (pathlib.Path(root) / "f.py").write_text("y = 1\n")
    before = len(_commits(root))
    rw._granular_commit(root, "f", "create")
    assert len(_commits(root)) == before     # nothing committed


def test_empty_stage_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_GRANULAR_COMMITS", "1")
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    from harness import ws_tx
    ws_tx.ensure_repo(root)
    before = len(_commits(root))
    rw._granular_commit(root, "cart", "repair")   # nothing changed
    assert len(_commits(root)) == before          # no empty commit
