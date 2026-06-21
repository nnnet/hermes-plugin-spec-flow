"""The product entry (assembly node) must NEVER build in an isolated worktree:
it is the serial integration point that wires every sibling into src/app.py and
runs alone at integrate. A worktree merge-back drops the brand-new src/app.py,
so the root integrate sees no entry and every route 404s. Regression for v057
('src/app.py never built' despite the leaf rebuilding it)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import run_engine as eng  # noqa: E402,F401
import spec_flow_runner as sfr  # noqa: E402


class _WS:
    enabled = True
    root = "/tmp/ws"


def _engine(isolation):
    e = sfr.Engine.__new__(sfr.Engine)
    e._isolation = isolation
    return e


def test_product_entry_never_isolated_under_worktree():
    e = _engine("worktree")
    assert e._leaf_uses_worktree("product_entry", _WS()) is False
    # a normal sibling leaf still gets its worktree
    assert e._leaf_uses_worktree("notes_api", _WS()) is True


def test_no_worktree_when_isolation_off():
    e = _engine("none")
    assert e._leaf_uses_worktree("notes_api", _WS()) is False
    assert e._leaf_uses_worktree("product_entry", _WS()) is False


def test_no_worktree_when_ws_disabled():
    e = _engine("worktree")

    class _Off:
        enabled = False
        root = None

    assert e._leaf_uses_worktree("notes_api", _Off()) is False
