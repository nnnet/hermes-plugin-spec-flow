"""Audit LAYER 3 — generated property scenarios (metamorphic probes).

Stages 1-10 pin KNOWN failure shapes on hand-picked cases; this layer probes
the charter (tests/audit/CHARTER.md) on GENERATED goals: a seeded RNG builds
a web project with 2-8 GET routes (names from a fixed dictionary) and 0-3
late requirements injected via ``standing_requirements``. Three metamorphic
properties, each its own test:

- P-A goal growth is monotone (P8/P3): a goal declaring MORE routes never
  yields a SMALLER task tree than the same goal's prefix.
- P-B collapse invariance (P3/P1): the collapsed (small-product floor) and
  the uncollapsed run of the SAME goal declare the SAME machine interface
  contract — collapsing structure must never change the declared contract.
- P-C a terminal verdict ALWAYS exists (P8/P7): whatever scenario the
  generator produces, ``run_project`` returns a RunResult with an honest
  READY / NOT READY — no exception, no hang (hard call ceiling, S8-style).

Fully offline: deterministic fake decomposer + the autonomous implementer,
fixture ``plugin`` from tests/conftest.py, engine via ``run_engine`` shim.
Seeds are FIXED; no unseeded randomness, no wall-clock dependence.
"""
import json
import pathlib
import random
import sys

import pytest

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import auto_implementer, run_engine as eng  # noqa: E402

SEEDS = [11, 23, 37, 59, 97]

# fixed vocabulary — route names the generator samples from
_ROUTE_WORDS = ["notes", "tasks", "items", "users", "events",
                "tags", "files", "posts", "labels", "boards"]
_LATE_WORDS = ["alpha", "beta", "gamma"]

_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}
_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def _goal(names: list[str]) -> str:
    routes = "; ".join(f"GET /{n} returns the {n} list" for n in names)
    return (f"A tiny read-only service over WSGI: {routes}."
            f" src/app.py exposes wsgi_app.")


def _project(names: list[str], **extra) -> dict:
    p = {"name": "prop-probe", "goal": _goal(names),
         "target": "every declared route answers in < 1s; stdlib only",
         "constitution": ["Standard library only."],
         "acceptance": {"smoke": ["the build succeeds"]},
         "policy": dict(_POLICY)}
    p.update(extra)
    return p


def _make_decomposer(names: list[str]):
    """Deterministic decomposer: root fans to one leaf per declared route."""
    kids = [{"id": f"{n}_get", "title": f"handle GET /{n}"} for n in names]

    def _d(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BIG), "children": [dict(k) for k in kids]}
        return {"metrics": dict(_SMALL)}
    return _d


class _Ceiling(BaseException):
    """Not an Exception — the engine's broad `except Exception` cannot swallow
    it, so a runaway loop fails the test deterministically instead of hanging."""


class _Counted:
    """Wrap a role worker; count calls on a SHARED per-run counter and raise
    _Ceiling once the total crosses the ceiling (S8-style convergence guard)."""

    def __init__(self, fn, counter: dict, ceiling: int):
        self._fn, self._counter, self._ceiling = fn, counter, ceiling

    def __call__(self, ctx):
        self._counter["n"] += 1
        if self._counter["n"] > self._ceiling:
            raise _Ceiling(
                f"agent calls exceeded {self._ceiling} — non-convergent")
        return self._fn(ctx)


class _LateReqs:
    """Standing-requirements source: silent on the first poll, then surfaces
    the generated late requirements once (the v146/v148 injection shape)."""

    def __init__(self, words: list[str]):
        self._words, self.polls = list(words), 0

    def __call__(self):
        self.polls += 1
        if self.polls <= 1 or not self._words:
            return []
        return [(f"late_{w}",
                 f"Add module {w}.py with def {w}() -> '{w[0]}'.")
                for w in self._words]


def _run(plugin, root, names, *, standing=None, extra=None, ceiling=400):
    counter = {"n": 0}
    agents = {
        "decomposer": _Counted(_make_decomposer(names), counter, ceiling),
        "implementer": _Counted(auto_implementer.implement, counter, ceiling),
    }
    res = eng.run_project(_project(names, **(extra or {})),
                          workspace=str(root), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents=agents, standing_requirements=standing)
    return res, counter["n"]


def _tree_size(res) -> int:
    def walk(node) -> int:
        return 1 + sum(walk(c) for c in (node.get("children") or []))
    tree = (res.project or {}).get("tree")
    assert tree, "a run must capture its task tree on project['tree']"
    return walk(tree)


def _declared_paths(res) -> set:
    """Route paths from the engine-written machine contract of this run."""
    root = res.workspace_root
    assert root, "a workspace run must expose workspace_root"
    contract = pathlib.Path(root) / "contracts" / "interface.json"
    assert contract.is_file(), (
        "the machine interface contract (contracts/interface.json) must be"
        " written for a product-depth web run — it is the ONE datum every"
        " consumer reads (charter P1)")
    data = json.loads(contract.read_text(encoding="utf-8"))
    return {r["path"] for r in data.get("routes", [])}


# ── P-A: goal growth is monotone ──────────────────────────────────────────────

@pytest.mark.parametrize("seed", SEEDS)
def test_goal_growth_is_monotone(plugin, tmp_path, seed):
    rng = random.Random(seed)
    n_small = rng.randint(2, 4)
    n_big = min(8, n_small + rng.randint(2, 4))
    names = rng.sample(_ROUTE_WORDS, n_big)
    res_small, _ = _run(plugin, tmp_path / "small", names[:n_small])
    res_big, _ = _run(plugin, tmp_path / "big", names)
    assert res_small is not None and res_big is not None
    sz_small, sz_big = _tree_size(res_small), _tree_size(res_big)
    assert sz_small <= sz_big, (
        f"a goal declaring {n_big} routes produced a SMALLER tree"
        f" ({sz_big} nodes) than its {n_small}-route prefix ({sz_small}"
        f" nodes) — growth of the goal must never shrink the plan")


# ── P-B: collapsed and uncollapsed runs declare the same contract ─────────────

@pytest.mark.parametrize("seed", SEEDS)
def test_collapse_preserves_declared_contract(plugin, tmp_path, seed):
    rng = random.Random(seed)
    n = rng.randint(2, 5)                    # inside the small-product floor
    names = rng.sample(_ROUTE_WORDS, n)
    expected = {f"/{w}" for w in names}
    # run A: default config — the small-product floor collapses the root
    res_a, _ = _run(plugin, tmp_path / "collapsed", names)
    # run B: floor disabled — the decomposer fan-out builds one leaf per route
    res_b, _ = _run(plugin, tmp_path / "expanded", names,
                    extra={"small_product_routes": 0})
    assert res_a is not None and res_b is not None
    paths_a, paths_b = _declared_paths(res_a), _declared_paths(res_b)
    assert paths_a == expected, (
        f"collapsed run declared {sorted(paths_a)}, goal declares"
        f" {sorted(expected)} — collapsing structure must not change the"
        f" contract (charter P3: complete replacement)")
    assert paths_b == expected, (
        f"expanded run declared {sorted(paths_b)}, goal declares"
        f" {sorted(expected)}")
    assert paths_a == paths_b, (
        "the SAME goal must declare the SAME interface contract regardless"
        " of collapsing (charter P1/P3)")


# ── P-C: a terminal verdict always exists ─────────────────────────────────────

@pytest.mark.parametrize("seed", SEEDS)
def test_terminal_verdict_always_exists(plugin, tmp_path, seed):
    rng = random.Random(seed)
    n_routes = rng.randint(2, 8)
    names = rng.sample(_ROUTE_WORDS, n_routes)
    n_late = rng.randint(0, 3)
    late = _LateReqs(rng.sample(_LATE_WORDS, n_late))
    ceiling = 400
    try:
        res, spent = _run(plugin, tmp_path / "wk", names,
                          standing=late, ceiling=ceiling)
    except _Ceiling as exc:
        pytest.fail(f"generated scenario (seed={seed}, routes={n_routes},"
                    f" late={n_late}) did NOT converge — {exc}")
    assert res is not None, "run_project must return a terminal RunResult"
    assert res.product_status in ("READY", "NOT READY"), (
        f"a product-depth run must end in an honest terminal verdict,"
        f" got {res.product_status!r} (seed={seed})")
    assert spent < ceiling, (
        f"generated scenario must converge well under {ceiling} agent calls,"
        f" spent {spent} (seed={seed})")
