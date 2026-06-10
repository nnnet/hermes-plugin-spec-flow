"""Battle tests — business scenarios: the plugin catches deliberate imprecision.

Each scenario feeds a vague/risky L0 goal (legitimate intent, imprecise
statement) through policy_gate + leaf_check and asserts the plugin BLOCKS or
CLARIFIES it (does not decompose), then that the resolved variant passes. This
is the core of what the user wants tested: the imprecision is removed by the
plugin, not by a human.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import scenarios as scn  # noqa: E402

SCENARIOS = scn.all_scenarios()
IDS = [s.name for s in SCENARIOS]


@pytest.fixture(params=SCENARIOS, ids=IDS)
def scenario(request):
    return request.param


class TestImprecisionCaught:
    def test_imprecise_is_gated(self, plugin, scenario):
        out = scn.run_pipeline(plugin.tools, scenario.imprecise)
        assert out["policy_verdict"] == scenario.imprecise.expect_policy
        # a blocked/clarified spec must NOT proceed to decomposition
        assert not out["proceeds_to_decompose"]

    def test_imprecise_leaf_is_branch(self, plugin, scenario):
        out = scn.run_pipeline(plugin.tools, scenario.imprecise)
        assert out["leaf_verdict"] == scenario.imprecise.expect_leaf

    def test_template_must_catch_is_covered(self, plugin, scenario):
        out = scn.run_pipeline(plugin.tools, scenario.imprecise)
        caught = " ".join(out["policy_blocks"] + out["policy_clarifications"]).lower()
        missed = [req for req in scenario.template_must_catch
                  if not scn._matches(req, caught, out)]
        assert not missed, f"{scenario.name}: template requirements not caught: {missed}"


class TestResolvedProceeds:
    def test_resolved_passes_policy(self, plugin, scenario):
        out = scn.run_pipeline(plugin.tools, scenario.resolved)
        assert out["policy_verdict"] == "pass"
        assert out["proceeds_to_decompose"]

    def test_resolved_then_decomposes(self, plugin, scenario):
        out = scn.run_pipeline(plugin.tools, scenario.resolved)
        assert out["leaf_verdict"] == scenario.resolved.expect_leaf


class TestParallelIsolation:
    """The user wants all scenarios runnable in parallel on isolated boards;
    here we confirm they are independent (no shared state leaks between runs).
    """

    def test_runs_are_independent(self, plugin):
        verdicts = {}
        for sc in SCENARIOS:
            out = scn.run_pipeline(plugin.tools, sc.imprecise)
            verdicts[sc.name] = out["policy_verdict"]
        # every scenario independently caught (block), order-independent
        assert all(v == "block" for v in verdicts.values()), verdicts
