"""Shim — re-export the plugin's production runner.

The run engine physically lives in the plugin (`spec_flow_runner.py`). It is
PLUGIN code now, not a harness; this module just loads it standalone (no Hermes
needed) and re-exports its names so existing tests / report.py keep using
`from harness import run_engine as eng`. It also supplies the test-only paths
(contracts dir, the openapi_diff validator, the default project) that the
domain-agnostic runner does not hardcode.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[2]   # spec-flow/
_HERE = Path(__file__).resolve().parent


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("spec_flow_tools")                 # standalone (guarded registry import)
_runner = _load("spec_flow_runner")
board = _load("spec_flow_board")         # offline kanban-execution core (Ф8)

# re-export everything from the runner
globals().update({k: getattr(_runner, k) for k in dir(_runner) if not k.startswith("__")})

# test-only locations the runner deliberately does not hardcode
CONTRACTS = _HERE.parent / "contracts"
OPENAPI_DIFF = _HERE / "openapi_diff.py"
RUNS_DIR = _HERE.parent / "runs"


def load_run(path=None):
    return _runner.load_run(path or (RUNS_DIR / "privacy_analytics.yaml"))


def run_scenario(case: dict, **kw):
    """Run a scenario the HONEST way: the plugin BUILDS the task tree itself by
    calling a deterministic decomposer over the case ``blueprint`` (the engine
    visits + gates every node, exactly like a live run). The scenario structure
    never drives the engine directly. ``oracle``/anchors stay analysis-only.

    A caller may still pass its own ``agents`` (e.g. an implementer, or a live
    ``decomposer``); only a missing decomposer is filled from the blueprint.
    """
    from harness import blueprint_decomposer as _bp
    bp = case.get("blueprint")
    exec_case = {k: v for k, v in case.items() if k != "blueprint"}
    agents = dict(kw.pop("agents", None) or {})
    if bp and "decomposer" not in agents:
        agents["decomposer"] = _bp.make(bp)
    return _runner.run_project(exec_case, agents=(agents or None), **kw)
