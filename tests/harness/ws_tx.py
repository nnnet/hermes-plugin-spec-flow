"""Workspace transactions on top of GIT — the industry's ready answer.

We are not the first to hit concurrent-writer corruption: the standard
for parallel coding agents is git-based isolation (worktree per agent,
sequential merge). Stage 1 here: the SHARED workspace becomes a git
repo and every mutation is a real transaction —

    with ws_tx.transaction(root, who="leaf:cart_api", why="write+bar") as tx:
        ...writes + verification...
        if worse: tx.rollback()      # exact restore, incl. NEW files

* isolation   — the commit queue serializes transactions;
* atomicity   — commit on success, full restore on rollback/exception
                (the hand-rolled snapshot missed newly created files);
* durability  — git history;
* attribution — every commit carries its author (who broke what is one
                `git log` away, forever).

Stage 2 (planned): worktree per leaf + sequential merge with
`git merge-tree` pre-flight — full isolation, engine-level surgery.

Every transaction logs event=ws_tx (commit hash / rollback) — each
firing of the safeguard is observable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import commit_queue

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": "/tmp/claude", "PATH": "/usr/bin:/bin:/usr/local/bin"}


def _git(root: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=root, env=_GIT_ENV,
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def ensure_repo(root: str) -> bool:
    """Idempotent: make the workspace a git repo with a baseline commit."""
    r = Path(root)
    if (r / ".git").exists():
        return True
    rc, out = _git(root, "init", "-q")
    if rc != 0:
        _log({"op": "init_failed", "detail": out[-200:]})
        return False
    (r / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n",
                                  encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=spec-flow", "-c", "user.email=tx@spec.flow",
         "commit", "-q", "-m", "workspace baseline", "--allow-empty")
    return True


class transaction:
    """One serialized, attributable, rollback-able workspace mutation."""

    def __init__(self, root: str, who: str, why: str):
        self.root, self.who, self.why = str(root), who, why
        self.rolled_back = False
        self._lock = commit_queue.exclusive(who, why)
        self._ok = ensure_repo(self.root)

    def __enter__(self):
        self._lock.__enter__()
        return self

    def rollback(self) -> None:
        """Exact restore to the pre-transaction state: modified files
        return, files CREATED inside the transaction are removed."""
        if not self._ok:
            return
        _git(self.root, "checkout", "-q", "--", ".")
        _git(self.root, "clean", "-fdq")
        self.rolled_back = True
        _log({"op": "rollback", "who": self.who, "why": self.why})

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None and not self.rolled_back:
                # an exception mid-write must not leave a half-commit
                self.rollback()
            elif not self.rolled_back and self._ok:
                _git(self.root, "add", "-A")
                rc, _ = _git(self.root, "diff", "--cached", "--quiet")
                if rc != 0:     # staged changes exist
                    _git(self.root, "-c", f"user.name={self.who}",
                         "-c", "user.email=tx@spec.flow",
                         "commit", "-q", "-m", f"{self.who}: {self.why}")
                    rc2, head = _git(self.root, "rev-parse", "--short",
                                     "HEAD")
                    _log({"op": "commit", "who": self.who, "why": self.why,
                          "rev": head.strip() if rc2 == 0 else "?"})
        finally:
            self._lock.__exit__(exc_type, exc, tb)
        return False


def _log(payload: dict) -> None:
    try:
        from . import llm_log
        llm_log.log({"event": "ws_tx", **payload})
    except Exception:               # noqa: BLE001 — logging never blocks work
        pass
