"""Audit STAGE 6 — termination / convergence caps.

v146 root cause: every per-node loop was bounded (max_rework, decompose cap,
integrate rework) but the SUM across nodes was not — a run could churn for an
hour without a terminal verdict. The engine now carries a GLOBAL run-call
budget (RUN_CALL_BUDGET / project["run_call_budget"]) enforced on every agent
call; crossing it raises RunExhausted and the run finalises as an HONEST
NOT READY. This stage pins all caps as finite-and-positive (static) and proves
the global budget actually halts a runaway run (dynamic — the offline v146
catcher).
"""
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402


# ── S6.1 per-node review rework is bounded, finite, > 0 ──────────────────────

def test_per_node_rework_cap_finite():
    cap = eng.DEFAULT_REVIEW_POLICY.get("max_rework")
    assert isinstance(cap, int) and 0 < cap < 100, (
        f"max_rework must be a small positive int, got {cap!r}")


# ── S6.2 decompose calls are globally bounded ────────────────────────────────

def test_decompose_calls_globally_bounded():
    cap = getattr(eng, "MAX_DECOMPOSE_CALLS", None)
    assert isinstance(cap, int) and 0 < cap < 10_000, (
        f"MAX_DECOMPOSE_CALLS must be finite and positive, got {cap!r}")


# ── S6.3 the WHOLE run has a global agent-call budget (v146) ─────────────────

def test_global_run_call_budget_exists_and_finite():
    budget = getattr(eng, "RUN_CALL_BUDGET", None)
    assert isinstance(budget, int) and 0 < budget < 100_000, (
        "the engine MUST carry a global run-call budget: per-node caps bound"
        " each node but their SUM is unbounded without it (v146, 46-min churn)."
        f" Got {budget!r}")
    # the budget must dominate the decompose cap, else decompose alone trips it
    assert budget > eng.MAX_DECOMPOSE_CALLS


def test_run_exhausted_is_a_run_stopper():
    # RunExhausted must exist and finalise like a cooperative stop (subclass),
    # so no broad `except Exception` inside node loops can swallow the halt.
    exc = getattr(eng, "RunExhausted", None)
    assert exc is not None and issubclass(exc, eng.RunStopped)


# ── S6.8 launcher attributability: every death leaves an exit-code record ────
# v148: the detached run was SIGKILLed externally and left NOTHING — no
# traceback, no terminal state, log frozen at startup. A run whose death cannot
# be attributed (engine exit vs external kill vs group sweep) is an
# observability hole: the ratchet cannot produce a rule from a ghost.

def test_launcher_records_exit_code():
    launcher = _TESTS / "run-detached.sh"
    text = launcher.read_text(encoding="utf-8")
    assert "rc=$?" in text and "KILLED by signal" in text, (
        "run-detached.sh must wrap python in a supervisor that logs the exit"
        " code (and decodes rc>=128 as a signal kill) — otherwise an external"
        " SIGKILL leaves an unattributable ghost death (v148)")


# ── S6.7 dynamic: a tiny budget HALTS a runaway run honestly ─────────────────
# (the offline v146 catcher — an always-failing coder with a 3-call budget
# must end in seconds with a terminal RunResult and an explicit budget FAIL
# event, never raise and never churn)

_PROJECT = {
    "name": "budget-probe",
    "goal": ("A tiny notes service over WSGI: POST /notes stores {text};"
             " GET /notes lists items. src/app.py exposes wsgi_app."),
    "target": "POST then GET round-trips a note; sqlite3 stdlib only",
    "constitution": ["Standard library only."],
    "acceptance": {"smoke": ["the build succeeds"]},
    "run_call_budget": 3,
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


def _never_passing_implementer(ctx):
    ws = ctx["workspace"]
    node = ctx.get("node", "x")
    ws._write(f"src/{eng._snake(node)}.py", "# intentionally empty\n", "adversarial")


def test_tiny_budget_halts_run_honestly(plugin, tmp_path):
    agents = {"decomposer": _fake_decomposer,
              "implementer": _never_passing_implementer}
    res = eng.run_project(dict(_PROJECT), workspace=str(tmp_path / "wk"),
                          depth="product", tools=plugin.tools,
                          contracts_dir=str(eng.CONTRACTS), agents=agents)
    assert res is not None, "budget exhaustion must yield a terminal RunResult"
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "budget exhausted" in dump, (
        "crossing run_call_budget must emit an explicit FAIL milestone —"
        " a silent halt would be a dishonest terminal")
