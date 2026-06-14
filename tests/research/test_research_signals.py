"""Battle test — extra research-trigger signal sources (roadmap E1).

The continuous-revision lane no longer fires only on completed-task count or
accumulated test errors. E1 adds three signal sources that close the
self-improvement loop on the PRODUCT, not just on the build:
  * acceptance failures (smoke/e2e/metric checks that did not pass),
  * metric regressions (a tracked acceptance metric degraded vs target),
  * an environment-rule change (law / PSP / policy shifted under us).
Each is independently sufficient to route work back to spec-research.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def _call(plugin, **kw):
    base = {"reason": "tick", "completed_tasks": 0, "test_errors": 0}
    base.update(kw)
    return json.loads(plugin.tools._handle_research_trigger_check(base))


def test_acceptance_errors_fire(plugin):
    out = _call(plugin, acceptance_errors=1)  # default k_acceptance_errors=1
    assert out["trigger"] is True
    assert any("acceptance_errors" in f for f in out["fired_by"])


def test_metric_regression_fires(plugin):
    out = _call(plugin, metric_regressions=1)  # default j_metric_regressions=1
    assert out["trigger"] is True
    assert any("metric_regressions" in f for f in out["fired_by"])


def test_env_rule_change_fires(plugin):
    out = _call(plugin, env_rule_changed=True)
    assert out["trigger"] is True
    assert "env_rule_change" in out["fired_by"]


def test_no_signal_no_fire(plugin):
    out = _call(plugin, completed_tasks=2, acceptance_errors=0,
                metric_regressions=0, env_rule_changed=False)
    assert out["trigger"] is False
    assert out["fired_by"] == []


def test_acceptance_delta_is_relative_to_baseline(plugin):
    # first acceptance failure fires and rebaselines
    first = _call(plugin, completed_tasks=1, acceptance_errors=1)
    assert first["trigger"] is True
    # same absolute count, past cooldown -> no NEW delta, no fire
    out = _call(plugin, completed_tasks=10, acceptance_errors=1)
    assert not any("acceptance_errors" in f for f in out["fired_by"])


def test_signals_reported_in_payload(plugin):
    out = _call(plugin, metric_regressions=3, env_rule_changed=True)
    assert out["signals"]["metric_regressions"] == 3
    assert out["signals"]["env_rule_changed"] is True


def test_thresholds_are_configurable(plugin, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_RESEARCH_LANE",
                       json.dumps({"k_acceptance_errors": 3}))
    below = _call(plugin, acceptance_errors=2)
    assert not any("acceptance_errors" in f for f in below["fired_by"])
    at = _call(plugin, acceptance_errors=3)
    assert any("acceptance_errors" in f for f in at["fired_by"])
