"""Battle test — spec version history + local-git provenance (roadmap Ф8.4 / E5).

A respec must never overwrite history: the superseded spec is archived as
``specs/<id>.v{N}.md`` with a ``superseded_by`` marker and the reason, the live
spec gets a revision-history stamp. With ``git_provenance=True`` every
milestone (leaf commit, respec) becomes a REAL commit in a local git repo
inside the workspace — the full rebuild history survives, no Hermes needed.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 70,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

REVISION = {
    "method": "level_return", "trigger": "on_level_return",
    "finding": "PSP rule change invalidates billing",
    "invalidates": "billing", "effect": "re-derive under the new rule",
}

PROJECT = {
    "name": "provenance-case",
    "goal": "a service whose respec history must survive",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "L0", "title": "Service", "metrics": _BRANCH,
        "children": [
            {"id": "billing", "title": "Billing", "metrics": _BRANCH,
             "children": [
                 {"id": "invoicing", "title": "Invoicing", "metrics": _LEAF},
             ]},
            {"id": "ledger", "title": "Ledger", "metrics": _LEAF},
        ],
    },
    "revisions": [REVISION],
}


def _run(plugin, tmp_path, **kw):
    return eng.run_project(dict(PROJECT), workspace=str(tmp_path / "wk"),
                           tools=plugin.tools, **kw)


# ─── spec version archive ─────────────────────────────────────────────


def test_superseded_spec_is_archived(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    root = pathlib.Path(res.workspace_root)
    archived = root / "specs" / "billing.v1.md"
    assert archived.is_file()
    text = archived.read_text(encoding="utf-8")
    assert "superseded_by: v2" in text
    assert "PSP rule change" in text


def test_live_spec_carries_revision_history(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    live = pathlib.Path(res.workspace_root) / "specs" / "billing.md"
    text = live.read_text(encoding="utf-8")
    assert "## Revision history" in text
    assert "v2 supersedes v1" in text


def test_untouched_specs_have_no_archive(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    specs = pathlib.Path(res.workspace_root) / "specs"
    assert not (specs / "ledger.v1.md").exists()
    assert "## Revision history" not in (specs / "ledger.md").read_text(encoding="utf-8")


def test_no_revision_no_archive(plugin, tmp_path):
    proj = {k: v for k, v in PROJECT.items() if k != "revisions"}
    res = eng.run_project(proj, workspace=str(tmp_path / "wk2"), tools=plugin.tools)
    specs = pathlib.Path(res.workspace_root) / "specs"
    assert not list(specs.glob("*.v*.md"))


# ─── local git provenance ─────────────────────────────────────────────

_GIT = shutil.which("git") is not None


@pytest.mark.skipif(not _GIT, reason="git not installed")
def test_git_provenance_records_real_commits(plugin, tmp_path):
    res = _run(plugin, tmp_path, depth="execute",
               agents={"implementer": __import__("harness.auto_implementer",
                                                 fromlist=["implement"]).implement},
               git_provenance=True)
    root = pathlib.Path(res.workspace_root)
    assert (root / ".git").is_dir()
    log = subprocess.run(["git", "-C", str(root), "log", "--oneline"],
                         capture_output=True, text=True)
    lines = log.stdout.strip().splitlines()
    assert len(lines) >= 2                           # leaf commits + respec
    full = log.stdout
    assert "respec(billing)" in full                 # the respec is a real commit


@pytest.mark.skipif(not _GIT, reason="git not installed")
def test_git_off_by_default(plugin, tmp_path):
    res = _run(plugin, tmp_path)
    assert not (pathlib.Path(res.workspace_root) / ".git").exists()


def test_git_failure_does_not_break_the_run(plugin, tmp_path, monkeypatch):
    # git "disappears" mid-run -> provenance silently degrades, run completes
    ws = eng._runner.Workspace(root=str(tmp_path / "wk3"), enabled=True,
                               git_provenance=True)
    monkeypatch.setattr(eng._runner.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert ws.git_commit("msg") is False             # degraded, no exception
