"""Audit STAGE 8 — dynamic simulation of the WHOLE control flow, offline.

Loop / convergence bugs live only where the state machine actually executes; a
static test cannot see them (v146: an amend re-ran the full leaf lifecycle
without converging, 46 min, no terminal). Here we drive a real product-depth run
with deterministic fake agents and a HARD call ceiling: whatever the agents do,
the engine MUST reach a terminal verdict within a bounded number of agent calls.
A runaway trips the ceiling (a BaseException the engine's `except Exception`
cannot swallow) and the test fails as non-convergent — in seconds, not an hour.
"""
import pathlib
import sys

import pytest

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import auto_implementer, run_engine as eng  # noqa: E402


# A p6-shaped web project: a root that fans to two route-owning leaves.
WEB_PROJECT = {
    "name": "micro-notes",
    "goal": ("A tiny notes service over WSGI: POST /notes stores {text} and"
             " returns {id}; GET /notes returns the items. src/app.py exposes"
             " wsgi_app."),
    "target": "POST then GET round-trips a note in < 1s; sqlite3 stdlib only",
    "constitution": ["Standard library only."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}
_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def _fake_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [{"id": "notes_post", "title": "handle POST /notes"},
                             {"id": "notes_get", "title": "handle GET /notes"}]}
    return {"metrics": dict(_SMALL)}


class _Ceiling(BaseException):
    """Not an Exception — the engine's broad `except Exception` cannot swallow
    it, so a runaway loop breaks the test deterministically instead of hanging."""


class _Counted:
    """Wrap a role worker; raise _Ceiling once total calls cross the ceiling."""
    _total = 0

    def __init__(self, fn, ceiling):
        self._fn, self._ceiling = fn, ceiling

    def __call__(self, ctx):
        type(self)._total += 1
        if type(self)._total > self._ceiling:
            raise _Ceiling(f"agent calls exceeded {self._ceiling} — non-convergent")
        return self._fn(ctx)


def _adversarial_implementer(ctx):
    """A coder that NEVER satisfies the gate: writes a module with no handler
    def and a failing test. The engine must bound the rework and TERMINATE
    (NOT READY honestly), never loop forever."""
    ws = ctx["workspace"]
    node = ctx.get("node", "x")
    ws._write(f"src/{eng._snake(node)}.py", "# intentionally empty — no handler\n",
              "adversarial")


def _run(plugin, root, agents, depth="product", acceptance=None,
         standing=None):
    project = dict(WEB_PROJECT)
    if acceptance is not None:
        project["acceptance"] = acceptance
    return eng.run_project(project, workspace=str(root), depth=depth,
                           tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                           agents=agents, standing_requirements=standing)


# ── S8.1 happy path — a full run converges in a bounded number of calls ───────

def test_full_run_converges_bounded(plugin, tmp_path):
    _Counted._total = 0
    ceiling = 200
    agents = {"decomposer": _Counted(_fake_decomposer, ceiling),
              "implementer": _Counted(auto_implementer.implement, ceiling)}
    res = _run(plugin, tmp_path / "wk", agents,
               acceptance={"smoke": ["the build succeeds"]})
    assert res is not None, "the run must return a terminal RunResult"
    assert _Counted._total < ceiling, (
        f"a healthy p6-scale run must converge well under {ceiling} agent calls,"
        f" spent {_Counted._total}")


# ── S8.2 adversarial — an always-failing coder must still TERMINATE ───────────

def test_adversarial_coder_still_terminates(plugin, tmp_path):
    _Counted._total = 0
    ceiling = 250
    agents = {"decomposer": _Counted(_fake_decomposer, ceiling),
              "implementer": _Counted(_adversarial_implementer, ceiling)}
    try:
        res = _run(plugin, tmp_path / "wk", agents,
                   acceptance={"smoke": ["server answers GET /health with 200"]})
    except _Ceiling as exc:
        pytest.fail(f"engine did NOT converge — {exc} (v146 class: unbounded"
                    " rework on a node that never satisfies its gate)")
    assert res is not None
    assert _Counted._total < ceiling, (
        f"even with a coder that never passes, the run must terminate under"
        f" {ceiling} calls (bounded rework), spent {_Counted._total}")


# ── S8.3 late amend injection must converge, not loop ─────────────────────────

def test_late_amend_injection_terminates_bounded(plugin, tmp_path):
    _Counted._total = 0
    ceiling = 250
    # a late requirement that EDITS an existing surface ("make the notes nice",
    # naming the owner file/route) — the v146 shape. It must route to amend and
    # terminate, never re-decompose in a loop.
    def _standing():
        return [("polish_notes",
                 "Make the notes nice to read: edit src/notes_get.py so GET"
                 " /notes renders an HTML <ul> list; no new route.")]
    agents = {"decomposer": _Counted(_fake_decomposer, ceiling),
              "implementer": _Counted(auto_implementer.implement, ceiling)}
    try:
        res = _run(plugin, tmp_path / "wk", agents,
                   acceptance={"smoke": ["the build succeeds"]},
                   standing=_standing)
    except _Ceiling as exc:
        pytest.fail(f"late amend did NOT converge — {exc}")
    assert res is not None
    assert _Counted._total < ceiling, (
        f"a late amend must converge under {ceiling} calls, spent"
        f" {_Counted._total}")
