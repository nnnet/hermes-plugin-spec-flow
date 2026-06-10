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
