"""Test harness for the spec-flow plugin.

The plugin is verifiable **without Hermes**: it imports ``tools.registry`` and
``toolsets`` (both stubbed here) and its seed commands shell to ``hermes
kanban`` but degrade gracefully when that binary is absent. We load the plugin
package fresh per test (hyphenated dir -> imported by file path under a
synthetic module name) so module-level registration runs against a clean fake
registry every time.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # spec-flow/
_PKG = "spec_flow_pkg"

# per-case run outputs contain deliberately-red test scaffolds — never collect
collect_ignore_glob = ["runs-out"]


def pytest_configure(config):
    """Seed the config floor from tests/.test.env BEFORE any harness module is
    imported during collection, so its (former) import-time defaults resolve
    from the file instead of hardcoded literals."""
    _tests_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_tests_dir))
    # Helper modules (live_dashboard, compare_runs, phase_overlap) moved under
    # tests/lib/ — keep them importable by bare name from any test subfolder.
    sys.path.insert(0, str(_tests_dir / "lib"))
    try:
        from harness import config as _cfg
        _cfg.load_test_env()
    except Exception:  # noqa: BLE001 — never block the run on env seeding
        pass


class FakeRegistry:
    """Captures registry.register(**kw) calls keyed by tool name."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register(self, **kw) -> None:
        self.tools[kw["name"]] = kw


def _install_fakes():
    reg = FakeRegistry()

    tools_pkg = types.ModuleType("tools")
    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = reg
    reg_mod.tool_error = lambda msg: json.dumps({"error": msg})
    tools_pkg.registry = reg_mod
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg_mod

    ts = types.ModuleType("toolsets")
    ts.TOOLSETS = {"kanban": {"tools": ["kanban_create"]}}
    sys.modules["toolsets"] = ts
    return reg, ts


def _load_plugin():
    for name in list(sys.modules):
        if name == _PKG or name.startswith(_PKG + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        _PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = pkg
    spec.loader.exec_module(pkg)
    return pkg


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Fresh plugin with fakes installed, HERMES_HOME pointed at a tmp dir, and
    register(ctx) already invoked so handlers + the tools module are available.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("SPEC_FLOW_RESEARCH_LANE", raising=False)
    reg, ts = _install_fakes()
    pkg = _load_plugin()
    pkg.register(object())  # triggers import of spec_flow_tools + registration
    tools = sys.modules[_PKG + ".spec_flow_tools"]
    return types.SimpleNamespace(pkg=pkg, reg=reg, ts=ts, tools=tools, tmp=tmp_path)


@pytest.fixture
def fake_openai():
    """Point the REAL backend at a REAL local OpenAI-compatible server (no
    monkeypatch). ``fake_openai(script, retries=3, backoff=0.01)`` starts a
    localhost endpoint serving ``script`` ([(status, body), ...]) and sets the
    backend CONFIG to it; the harness's real HTTP path runs against it. Config
    is restored on teardown. See tests/harness_fakeapi.py."""
    from harness import llm_backend as lb
    from harness_fakeapi import FakeOpenAI

    started = []
    keys = ("BASE_URL", "API_KEY", "RETRIES", "BACKOFF", "BACKEND")
    saved = {k: getattr(lb, k) for k in keys}

    def _make(script, *, retries=3, backoff=0.01):
        srv = FakeOpenAI(script).__enter__()
        started.append(srv)
        lb.BASE_URL, lb.API_KEY = srv.base_url, "test-key"
        lb.RETRIES, lb.BACKOFF, lb.BACKEND = retries, backoff, "openai"
        return srv

    yield _make
    for k, v in saved.items():
        setattr(lb, k, v)
    for srv in started:
        srv.__exit__(None, None, None)
