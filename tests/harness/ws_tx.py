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
    # DEBUG (SPEC_FLOW_GIT_TRACE): a reference-transaction hook logs every
    # committed move of master/HEAD with its reflog action — reveals WHO resets
    # the ref (commit / reset / checkout / branch) when a leaf's code is orphaned.
    import os
    if os.environ.get("SPEC_FLOW_GIT_TRACE"):
        hooks = r / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hk = hooks / "reference-transaction"
        _log_abs = str((r / ".git" / "ref-trace.log").resolve())
        hk.write_text(
            "#!/bin/sh\n"
            '[ "$1" = committed ] || exit 0\n'
            "while read old new ref; do\n"
            '  case "$ref" in *master|HEAD)\n'
            '    printf "%s %s %s->%s [%s]\\n" "$(date +%H:%M:%S)" "$ref" '
            f'"$old" "$new" "$GIT_REFLOG_ACTION" >> "{_log_abs}" ;;\n'
            "  esac\n"
            "done\n", encoding="utf-8")
        os.chmod(hk, 0o755)
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


# ─── Stage 2 (axis F): worktree per leaf + merge-tree pre-flight ───────
# Physical isolation: a leaf builds in its OWN git worktree on its OWN
# branch — it can never see or clobber a sibling's half-written files
# (anti-dup axes A/B/C reduce LOGICAL overlap; F removes PHYSICAL
# overlap). Re-integration is serialized and guarded: before a leaf
# branch is merged back, `git merge-tree` REPLAYS the merge in memory and
# refuses it on conflict, so a bad merge never lands in the workspace.

import shutil   # noqa: E402
import tempfile  # noqa: E402


def _branch_name(leaf: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in leaf)
    return f"leaf/{safe.strip('-') or 'node'}"


def merge_tree_preflight(root: str, ours: str, theirs: str) -> dict:
    """Replay merging `theirs` into `ours` IN MEMORY (no working-tree
    change). Returns {"clean": bool, "conflicts": [paths]}. `git
    merge-tree --write-tree` exits 0 when the merge is clean, 1 when it
    conflicts; with --name-only the conflicting paths head the output."""
    rc, out = _git(root, "merge-tree", "--write-tree", "--name-only",
                   ours, theirs)
    if rc == 0:
        return {"clean": True, "conflicts": []}
    lines = out.splitlines()
    conflicts = []
    for ln in lines[1:]:             # line 0 is the (unusable) tree oid
        ln = ln.strip()
        if not ln or ln.startswith(("Auto-merging", "CONFLICT", "warning:")):
            break
        conflicts.append(ln)
    return {"clean": False, "conflicts": conflicts}


class leaf_worktree:
    """Isolate one leaf in its own worktree + branch; merge back on a
    clean exit, guarded by a merge-tree pre-flight.

        with ws_tx.leaf_worktree(root, "cart_api", "leaf:cart_api") as wt:
            ...write into wt.path...
        wt.merged    # True when its branch landed in the workspace
        wt.conflicts # [paths] when the pre-flight refused the merge

    The worktree path and branch are always cleaned up. The MERGE step is
    serialized through the commit queue so concurrent leaves integrate one
    at a time — a sibling's commit shifts HEAD, and the next leaf's
    pre-flight runs against that NEW HEAD."""

    def __init__(self, root: str, leaf: str, who: str):
        self.root, self.leaf, self.who = str(root), leaf, who
        self.branch = _branch_name(leaf)
        self.path: str = ""
        self.merged = False
        self.conflicts: list = []
        self._ok = ensure_repo(self.root)

    def __enter__(self) -> "leaf_worktree":
        if not self._ok:
            # no git → degrade to writing straight into the workspace
            self.path = self.root
            return self
        self.path = tempfile.mkdtemp(prefix="spec-flow-wt-")
        # a fresh branch off the CURRENT workspace HEAD, checked out in the
        # isolated worktree. -f tolerates a stale branch from a prior run.
        _git(self.root, "branch", "-f", self.branch, "HEAD")
        rc, out = _git(self.root, "worktree", "add", "-f",
                       self.path, self.branch)
        if rc != 0:
            _log({"op": "worktree_add_failed", "who": self.who,
                  "detail": out[-200:]})
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = self.root     # degrade rather than fail the leaf
            self._ok = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._ok:
            return False
        try:
            # Commit whatever the leaf left UNCOMMITTED to its own branch.
            _git(self.path, "add", "-A")
            rc, _ = _git(self.path, "diff", "--cached", "--quiet")
            if rc != 0 and exc_type is None:
                _git(self.path, "-c", f"user.name={self.who}",
                     "-c", "user.email=tx@spec.flow",
                     "commit", "-q", "-m", f"{self.who}: leaf work")
            # Integrate when the branch carries ANY work ahead of the workspace
            # HEAD. This covers BOTH the changes just committed above AND the
            # commits the leaf's OWN ws_tx.transaction already made INSIDE the
            # worktree (the orchestra / write+bar path commits there directly).
            # The former gate merged only when leaf_worktree ITSELF committed —
            # so a leaf that committed its code internally left `git add -A`
            # with nothing staged, _integrate() was skipped, and _cleanup()
            # then deleted the branch, orphaning the leaf's real src before it
            # ever reached master. That is the v078/v079 NOT-READY root cause:
            # master kept only the runner's spec-only `feat:` commits while
            # every leaf's code was silently dropped.
            if exc_type is None:
                _, ahead = _git(self.root, "rev-list", "--count",
                                f"HEAD..{self.branch}")
                if ahead.strip().isdigit() and int(ahead.strip()) > 0:
                    self._integrate()
        finally:
            self._cleanup()
        return False                  # never swallow the leaf's exception

    def _integrate(self) -> None:
        """Serialized, pre-flighted merge of this leaf's branch into the
        workspace. A conflict is reported, NOT forced."""
        with commit_queue.exclusive(self.who, "leaf merge"):
            # The workspace tree may carry the runner's freshly-written but
            # still-UNTRACKED files (e.g. specs/<leaf>.md the engine drops on
            # the main tree). `git merge` refuses to overwrite untracked files
            # and aborts ("would be overwritten by merge") — which silently
            # orphaned the leaf branch again. Commit the current workspace
            # state first so the merge is a clean 3-way against tracked content
            # (identical specs collapse to a no-op; a genuine clash is then
            # surfaced by the pre-flight, not a raw merge abort).
            _git(self.root, "add", "-A")
            _rc, _ = _git(self.root, "diff", "--cached", "--quiet")
            if _rc != 0:
                _git(self.root, "-c", f"user.name={self.who}",
                     "-c", "user.email=tx@spec.flow", "commit", "-q", "-m",
                     f"workspace state before {self.branch} merge")
            pf = merge_tree_preflight(self.root, "HEAD", self.branch)
            if not pf["clean"]:
                self.conflicts = pf["conflicts"]
                _log({"op": "merge_conflict", "who": self.who,
                      "branch": self.branch, "conflicts": self.conflicts})
                return
            rc, out = _git(self.root, "-c", f"user.name={self.who}",
                           "-c", "user.email=tx@spec.flow",
                           "merge", "--no-ff", "-q", "-m",
                           f"merge {self.branch}", self.branch)
            if rc == 0:
                self.merged = True
                _, head = _git(self.root, "rev-parse", "--short", "HEAD")
                _log({"op": "merge", "who": self.who, "branch": self.branch,
                      "rev": head.strip()})
            else:                     # pre-flight said clean but merge balked
                _git(self.root, "merge", "--abort")
                self.conflicts = ["<merge-failed>"]
                _log({"op": "merge_failed", "who": self.who,
                      "detail": out[-200:]})

    def _cleanup(self) -> None:
        if self.path and self.path != self.root:
            _git(self.root, "worktree", "remove", "-f", self.path)
            shutil.rmtree(self.path, ignore_errors=True)
        _git(self.root, "branch", "-D", self.branch)
