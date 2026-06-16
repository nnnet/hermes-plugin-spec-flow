"""Checkpoints — snapshot WHERE a run is so it can be REPLAYED from there.

A checkpoint is a full copy of the workspace (specs/src/tests + journal) taken
at a node boundary, keyed by a digest of all live specs. The engine can write
them itself on a cadence (SPEC_FLOW_CHECKPOINT_EVERY — a run parameter, like the
decomposer type) or on request; restore + a --resume re-enters from exactly that
point so any saved checkpoint is replayable, not just the latest.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402

_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}
_BRANCH = {"modules": 2, "tasks": 6, "interfaces": 2, "estimated_loc": 200,
           "open_decisions": 0, "single_concern": False, "testable_criteria": True}


def _project():
    return {
        "name": "ck", "goal": "tiny", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 0,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": "a", "title": "A", "metrics": dict(_LEAF)},
                              {"id": "b", "title": "B", "metrics": dict(_LEAF)}]},
    }


# ── snapshot / restore round-trip ────────────────────────────────────────

def test_snapshot_excludes_sentinels_and_restores(tmp_path):
    ws = tmp_path / "run" / "workspace"
    (ws / "specs").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / ".spec-flow").mkdir()
    (ws / "specs" / "a.md").write_text("# a\n", encoding="utf-8")
    (ws / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".spec-flow" / "journal.jsonl").write_text(
        '{"node":"a","version":1}\n', encoding="utf-8")
    (ws / ".spec-flow" / "CHECKPOINT").write_text("c\n", encoding="utf-8")

    ck = eng.snapshot_checkpoint(str(tmp_path / "run"), str(ws), node="a")
    ckp = pathlib.Path(ck)
    meta = json.loads((ckp / "checkpoint.json").read_text(encoding="utf-8"))
    assert meta["seq"] == 1 and meta["node"] == "a" and meta["specs"] == 1
    # the volatile sentinel is never frozen into a snapshot
    assert not (ckp / "workspace" / ".spec-flow" / "CHECKPOINT").exists()
    assert (ckp / "workspace" / "specs" / "a.md").exists()

    dest = tmp_path / "run2" / "workspace"
    eng.restore_checkpoint(ck, str(dest))
    assert (dest / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (dest / ".spec-flow" / "journal.jsonl").exists()


def test_root_spec_hash_stable_and_sensitive(tmp_path):
    ws = tmp_path / "ws"
    (ws / "specs").mkdir(parents=True)
    (ws / "specs" / "a.md").write_text("one\n", encoding="utf-8")
    h1 = eng._root_spec_hash(str(ws))
    assert h1 == eng._root_spec_hash(str(ws))          # stable
    (ws / "specs" / "a.md").write_text("two\n", encoding="utf-8")
    assert eng._root_spec_hash(str(ws)) != h1          # content-sensitive
    # archived versions never perturb the id
    (ws / "specs" / "a.v1.md").write_text("old\n", encoding="utf-8")
    assert eng._root_spec_hash(str(ws)) == eng._root_spec_hash(str(ws))


def test_list_checkpoints_orders_by_seq(tmp_path):
    ws = tmp_path / "run" / "workspace"
    (ws / "specs").mkdir(parents=True)
    (ws / "specs" / "a.md").write_text("a\n", encoding="utf-8")
    eng.snapshot_checkpoint(str(tmp_path / "run"), str(ws), node="a")
    (ws / "specs" / "b.md").write_text("b\n", encoding="utf-8")
    eng.snapshot_checkpoint(str(tmp_path / "run"), str(ws), node="b")
    cks = eng.list_checkpoints(str(tmp_path / "run"))
    assert [c["seq"] for c in cks] == [1, 2]
    assert cks[1]["node"] == "b"


# ── engine auto-checkpoint cadence (the run parameter) ───────────────────

def test_engine_auto_checkpoints_on_cadence(tmp_path, monkeypatch):
    # SPEC_FLOW_CHECKPOINT_EVERY=1 → the plugin snapshots itself at every node
    # boundary, with no external command. Off by default (other cases unchanged).
    monkeypatch.setenv("SPEC_FLOW_CHECKPOINT_EVERY", "1")
    run_dir = tmp_path / "run"
    eng.run_project(_project(), workspace=str(run_dir / "workspace"),
                    depth="spec")
    cks = eng.list_checkpoints(str(run_dir))
    assert len(cks) >= 1, "engine should have auto-snapshotted at a boundary"


def test_no_auto_checkpoint_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_CHECKPOINT_EVERY", raising=False)
    run_dir = tmp_path / "run"
    eng.run_project(_project(), workspace=str(run_dir / "workspace"),
                    depth="spec")
    assert eng.list_checkpoints(str(run_dir)) == []


# ── replay: restore a checkpoint + resume skips its done nodes ────────────

def test_replay_from_checkpoint_reuses_done_nodes(tmp_path):
    calls = {"n": 0}

    def implementer(ctx):
        calls["n"] += 1
        root = pathlib.Path(ctx["workspace"].root)
        (root / "src").mkdir(parents=True, exist_ok=True)
        mod = ctx.get("module") or ctx["node"]
        (root / "src" / f"{mod}.py").write_text("x = 1\n", encoding="utf-8")
        return {"files": [f"src/{mod}.py"]}

    run1 = tmp_path / "run1"
    eng.run_project(_project(), workspace=str(run1 / "workspace"),
                    depth="execute", agents={"implementer": implementer})
    ck = eng.snapshot_checkpoint(str(run1), str(run1 / "workspace"), node="L0")
    built_first = calls["n"]
    assert built_first >= 1

    # replay: restore the snapshot into a fresh run dir and resume — the done
    # leaves (unchanged specs, journalled) must be cache-hits, not rebuilt
    calls["n"] = 0
    run2 = tmp_path / "run2"
    eng.restore_checkpoint(ck, str(run2 / "workspace"))
    eng.run_project(_project(), workspace=str(run2 / "workspace"),
                    depth="execute", resume=True,
                    agents={"implementer": implementer})
    assert calls["n"] == 0, "resume from checkpoint must reuse the built leaves"
