"""Commit queue + leaf time ceiling: the v17 reliability pair.

v17 post-mortem: the engine declared the root DONE while the final suite
NEVER went green — concurrent workspace writers produced a module/test
pair no single writer ever wrote, every repair rolled back, and the
record policy hid the red root behind '✅ завершён'."""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import commit_queue  # noqa: E402
from harness import run_engine as eng  # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


def _project(**extra):
    proj = {
        "name": "cq-case", "goal": "g", "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 1,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": "a", "title": "Catalog browsing",
                               "metrics": dict(_LEAF)},
                              {"id": "b", "title": "Payment capture",
                               "metrics": dict(_LEAF)}]},
    }
    proj.update(extra)
    return proj


# ─── the queue itself ─────────────────────────────────────────────────

def test_exclusive_serializes_writers():
    state = {"active": 0, "peak": 0, "done": 0}
    guard = threading.Lock()

    def writer(i):
        with commit_queue.exclusive(f"w{i}", "test"):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.03)
            with guard:
                state["active"] -= 1
                state["done"] += 1

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1, "two writers held the queue at once"
    assert state["done"] == 5


def test_exclusive_is_reentrant():
    with commit_queue.exclusive("outer", "test"):
        with commit_queue.exclusive("outer", "nested helper"):
            pass    # a deadlock here would hang the suite


def test_exclusive_releases_on_error():
    try:
        with commit_queue.exclusive("boom", "test"):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    # the lock must be free again
    acquired = commit_queue._LOCK.acquire(timeout=1)
    assert acquired
    commit_queue._LOCK.release()


# ─── leaf time ceiling (limits.leaf_seconds) ──────────────────────────

def test_leaf_timeout_surrenders_leaf_not_run():
    def slow_impl(ctx):
        # a worker honouring its deadline raises TimeoutError at a
        # round boundary — the engine must surrender the LEAF only
        raise TimeoutError("leaf time ceiling reached before repair round")

    res = eng.run_project(_project(limits={"leaf_seconds": 1}),
                          workspace="/tmp/claude/cq-wk1", depth="execute",
                          agents={"implementer": slow_impl})
    timeouts = [lp for lp in res.loops if lp["type"] == "leaf-timeout"]
    assert timeouts, "TimeoutError must be recorded as leaf-timeout"
    miles = [e for e in res.events if e.gate == "leaf_timeout"]
    assert miles and miles[0].verdict == "TIMEOUT"


def test_deadline_present_in_ctx_only_with_limit():
    seen = {}

    def impl(ctx):
        seen[ctx["node"]] = ctx.get("deadline")

    eng.run_project(_project(limits={"leaf_seconds": 60}),
                    workspace="/tmp/claude/cq-wk2", depth="execute",
                    agents={"implementer": impl})
    assert seen and all(v and v > time.time() for v in seen.values())

    seen.clear()
    eng.run_project(_project(), workspace="/tmp/claude/cq-wk3",
                    depth="execute", agents={"implementer": impl})
    assert seen and all(v is None for v in seen.values())
