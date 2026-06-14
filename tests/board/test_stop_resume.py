"""П1: cooperative stop + resume. A STOP sentinel halts the run at the
next node boundary and returns a PARTIAL result; a later resume re-enters
the same workspace, reuses the journaled leaves, and continues. The stop
is file-based so an out-of-process --stop can request it."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402
import spec_flow_journal as journal  # noqa: E402  (repo root on path via run_engine)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import spec_flow_runner as rnr  # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 1,
           "human_in_loop": False, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


# distinct, non-similar concerns so the dedup gate keeps all five
_LEAVES = [("seller_onboarding", "Seller onboarding"),
           ("buyer_cart", "Buyer cart"),
           ("payment_payout", "Payment payout"),
           ("catalog_search", "Catalog search"),
           ("order_tracking", "Order tracking")]


def _five_leaf_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BRANCH), "children": [
            {"id": i, "title": t} for i, t in _LEAVES]}
    return {"metrics": dict(_LEAF)}


def _project():
    return {"name": "stop-case", "goal": "g", "target": "x", "policy": _POLICY}


# ─── the stop sentinel helpers ────────────────────────────────────────

def test_request_and_clear_stop(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir()
    p = rnr.request_stop(root)
    assert pathlib.Path(p).is_file()
    rnr.clear_stop(root)
    assert not pathlib.Path(p).exists()
    rnr.clear_stop(root)            # idempotent — no error when already gone


# ─── cooperative stop returns a partial result ────────────────────────

def test_stop_halts_with_partial_result(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth="scaffold",
                   agents={"decomposer": _five_leaf_decomposer})
    # stop once the first leaf has committed — the next node boundary halts
    e._stop_requested = lambda: e._completed >= 1
    res = e.run(_project())
    assert e._stopped is True
    # a STOPPED milestone was emitted and a run_stopped wave recorded
    assert any(getattr(ev, "verdict", "") == "STOPPED" for ev in e.events)
    wpath = tmp_path / "wk" / ".spec-flow" / "waves.jsonl"
    stopped = list(journal.RunJournal(wpath).entries(kind="run_stopped"))
    assert stopped, "the stop must be journaled"
    # NOT every leaf finished — the run was cut short
    done = e.workspace.journal_nodes()
    assert len(done) < 5
    assert res is not None          # a partial RunResult, not a crash


# ─── a stale STOP never halts a fresh/resumed run ─────────────────────

def test_clear_stop_at_run_start(tmp_path):
    root = str(tmp_path / "wk")
    pathlib.Path(root).mkdir(parents=True)
    # a stale sentinel from a previous run sits in the workspace
    rnr.request_stop(root)
    e = eng.Engine(workspace=root, depth="scaffold",
                   agents={"decomposer": _five_leaf_decomposer})
    e.run(_project())
    # run() cleared the stale STOP, so the run finished all five leaves
    assert e._stopped is False
    assert len(e.workspace.journal_nodes()) == 5


# ─── resume re-enters and reuses the journaled leaves ─────────────────

def test_resume_loads_the_journal(tmp_path):
    root = str(tmp_path / "wk")
    # run 1: complete the whole tree — every leaf is journaled
    e1 = eng.Engine(workspace=root, depth="scaffold",
                    agents={"decomposer": _five_leaf_decomposer})
    e1.run(_project())
    assert len(e1.workspace.journal_nodes()) == 5

    # run 2 (resume): the engine loads the prior journal so a restart knows
    # which leaves already finished
    e2 = eng.Engine(workspace=root, depth="scaffold", resume=True,
                    agents={"decomposer": _five_leaf_decomposer})
    e2.run(_project())
    assert e2._journal_done and len(e2._journal_done) == 5
