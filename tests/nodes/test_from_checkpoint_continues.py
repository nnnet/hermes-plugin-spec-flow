"""End-to-end of the operator loop the dashboard drives: clone a past run from
a checkpoint M into a FRESH workspace and resume — the new run must CONTINUE
from M, not restart. Concretely: on resume the decomposer is NOT re-run (the
tree is restored from the persisted decomposition, so node ids stay stable) and
only the leaves not yet done at M are (re)implemented; earlier leaves are
reused from the journal. Without the fix a resume re-decomposes from L0, drifts
the ids, and silently rebuilds the whole tree."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import spec_flow_runner as sfr   # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 1,
           "human_in_loop": False, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}
_LEAVES = ["alpha_leaf", "beta_leaf", "gamma_leaf"]


def _decomp(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BRANCH),
                "children": [{"id": n, "title": n} for n in _LEAVES]}
    return {"metrics": dict(_LEAF),
            "spec_markdown": f"## Requirements\n- {ctx['node'].get('title')}"}


def _run(workspace, resume, log):
    def impl(ctx):
        log.append(ctx["node"])
        ws = ctx["workspace"]
        fn = ctx["module"]
        ws._write(f"src/{fn}.py", f"X = '{fn}'\n", "code")
        ws._write(f"tests/test_{fn}.py", "def test_x():\n    assert True\n", "test")
    e = sfr.Engine(workspace=workspace, depth="execute", resume=resume,
                   agents={"decomposer": _decomp, "implementer": impl})
    e.run({"name": "c", "goal": "g", "target": "x", "policy": _POLICY})
    return e


def _journal_nodes(checkpoint_dir):
    j = pathlib.Path(checkpoint_dir) / "workspace" / ".spec-flow" / "journal.jsonl"
    import json
    done = set()
    if j.is_file():
        for line in j.read_text().splitlines():
            try:
                done.add(json.loads(line)["node"])
            except Exception:
                pass
    return done


def test_clone_from_checkpoint_continues_not_restarts(tmp_path, monkeypatch):
    run1 = tmp_path / "run1"
    ws1 = str(run1 / "workspace")
    # snapshot at every node boundary so a mid-run checkpoint exists
    monkeypatch.setenv("SPEC_FLOW_CHECKPOINT_EVERY", "1")
    first = []
    e1 = _run(ws1, False, first)
    assert e1._decompose_calls > 0                  # run1 built the tree
    assert set(first) == set(_LEAVES)               # all leaves implemented once

    cks = sfr.list_checkpoints(str(run1))
    assert cks, "auto-checkpoints must have been written"
    # pick the checkpoint where EXACTLY ONE leaf was already done — resuming it
    # must finish the OTHER two and skip the done one (continue from M)
    target, done_at_m = None, set()
    for ck in cks:
        d = _journal_nodes(ck["path"]) & set(_LEAVES)
        if len(d) == 1:
            target, done_at_m = ck, d
            break
    assert target, f"need a checkpoint with one leaf done; got {cks}"

    # clone: restore M into a FRESH run dir, then resume there (run1 untouched)
    run2 = tmp_path / "run2"
    ws2 = str(run2 / "workspace")
    sfr.restore_checkpoint(target["path"], ws2)
    monkeypatch.setenv("SPEC_FLOW_CHECKPOINT_EVERY", "0")
    second = []
    e2 = _run(ws2, True, second)

    # the nodes planned BEFORE M are restored from the journal, not re-asked:
    # L0 (the root) in particular must be restored, never re-decomposed — that
    # is what kept the ids stable. Nodes after M may still decompose fresh
    # (that is the continuation), but strictly FEWER than a full rebuild.
    restored = [e for e in e2.events
                if e.phase == "decompose"
                and "restored this level from the run journal" in e.action]
    assert any(e.task == "L0" for e in restored), "L0 must be restored, not re-decomposed"
    assert e2._decompose_calls < e1._decompose_calls   # not a full rebuild from L0
    # continues from M: the leaf already done at M is reused (not re-implemented),
    # only the remaining leaves finish
    assert done_at_m and not (set(second) & done_at_m)
    assert set(second) == set(_LEAVES) - done_at_m
