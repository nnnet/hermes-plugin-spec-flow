"""Autonomous tests for the spec-flow plugin — no Hermes required.

Covers registration/toolset wiring, the three deterministic gates
(leaf_check, contract_check, research_trigger_check) and the seed commands
(specflow_init/start/status), including graceful degradation when the
``hermes`` binary is absent.
"""

from __future__ import annotations

import json
import os

import pytest

EXPECTED_TOOLS = {
    "leaf_check",
    "contract_check",
    "research_trigger_check",
    "policy_gate",
    "run_report",
    "specflow_init",
    "specflow_start",
    "specflow_status",
}


def _j(s: str) -> dict:
    return json.loads(s)


# ---------------------------------------------------------------------------
# Registration / toolset wiring
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_all_tools_registered(self, plugin):
        assert set(plugin.reg.tools) == EXPECTED_TOOLS

    def test_tools_surface_in_kanban_toolset(self, plugin):
        kanban = plugin.ts.TOOLSETS["kanban"]["tools"]
        for name in EXPECTED_TOOLS:
            assert name in kanban
        # pre-existing entry preserved
        assert "kanban_create" in kanban

    def test_registration_metadata(self, plugin):
        for name, kw in plugin.reg.tools.items():
            assert kw["toolset"] == "kanban"
            assert callable(kw["handler"])
            assert callable(kw["check_fn"])
            assert kw["emoji"]
            # schema is a valid function-calling dict naming the tool
            fn = kw["schema"]["function"]
            assert kw["schema"]["type"] == "function"
            assert fn["name"] == name
            assert fn["parameters"]["type"] == "object"

    def test_register_is_idempotent(self, plugin):
        before = list(plugin.ts.TOOLSETS["kanban"]["tools"])
        plugin.pkg.register(object())
        after = list(plugin.ts.TOOLSETS["kanban"]["tools"])
        assert before == after  # no duplicates added on a second call

    def test_register_survives_missing_kanban_toolset(self, plugin):
        # Drop the kanban toolset entirely; register must not raise.
        plugin.ts.TOOLSETS.pop("kanban", None)
        plugin.pkg.register(object())  # should warn internally, not crash


# ---------------------------------------------------------------------------
# leaf_check
# ---------------------------------------------------------------------------

class TestLeafCheck:
    def call(self, plugin, **kw):
        base = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 40}
        base.update(kw)
        return _j(plugin.tools._handle_leaf_check(base))

    def test_clean_leaf(self, plugin):
        out = self.call(plugin)
        assert out["verdict"] == "leaf"
        assert out["reasons"] == []

    def test_too_many_modules_branches(self, plugin):
        out = self.call(plugin, modules=2)
        assert out["verdict"] == "branch"
        assert any("modules" in r for r in out["reasons"])

    def test_loc_threshold_branches(self, plugin):
        out = self.call(plugin, estimated_loc=300)
        assert out["verdict"] == "branch"
        assert any("estimated_loc" in r for r in out["reasons"])

    def test_coupled_single_concern_branches(self, plugin):
        out = self.call(plugin, single_concern=False)
        assert out["verdict"] == "branch"
        assert any("coupled" in r for r in out["reasons"])

    def test_open_decisions_branches(self, plugin):
        out = self.call(plugin, open_decisions=1)
        assert out["verdict"] == "branch"

    def test_non_testable_branches(self, plugin):
        out = self.call(plugin, testable_criteria=False)
        assert out["verdict"] == "branch"

    def test_thresholds_reported(self, plugin):
        out = self.call(plugin)
        assert out["thresholds"]["max_loc"] == plugin.tools.MAX_LOC


# ---------------------------------------------------------------------------
# contract_check
# ---------------------------------------------------------------------------

class TestContractCheck:
    def test_requires_contract(self, plugin):
        out = _j(plugin.tools._handle_contract_check({"contract_artifacts": []}))
        assert "error" in out

    def test_missing_validator_is_unavailable(self, plugin, monkeypatch):
        # Default validators (redocly/...) are absent in CI -> unavailable.
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        out = _j(plugin.tools._handle_contract_check(
            {"contract_artifacts": ["openapi.yaml"], "types": ["openapi"]}
        ))
        assert out["unavailable"]
        assert out["status"] == "ok"  # lax mode: unavailable does not fail

    def test_strict_fails_on_unavailable(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        out = _j(plugin.tools._handle_contract_check(
            {"contract_artifacts": ["openapi.yaml"], "types": ["openapi"], "strict": True}
        ))
        assert out["status"] == "drift"

    def test_clean_validator_passes(self, plugin, monkeypatch):
        # Use the always-success `true` binary as a stand-in validator.
        monkeypatch.setitem(plugin.tools.CONTRACT_VALIDATORS, "openapi", ["true", "{contract}"])
        out = _j(plugin.tools._handle_contract_check(
            {"contract_artifacts": ["openapi.yaml"], "types": ["openapi"]}
        ))
        assert out["status"] == "ok"
        assert out["validated"] and not out["drift"]

    def test_failing_validator_is_drift(self, plugin, monkeypatch):
        # `false` always exits non-zero -> reported as drift.
        monkeypatch.setitem(plugin.tools.CONTRACT_VALIDATORS, "openapi", ["false", "{contract}"])
        out = _j(plugin.tools._handle_contract_check(
            {"contract_artifacts": ["openapi.yaml"], "types": ["openapi"]}
        ))
        assert out["status"] == "drift"
        assert out["drift"]

    def test_parallel_types_all_run(self, plugin, monkeypatch):
        monkeypatch.setitem(plugin.tools.CONTRACT_VALIDATORS, "openapi", ["true", "{contract}"])
        monkeypatch.setitem(plugin.tools.CONTRACT_VALIDATORS, "zod", ["true", "{contract}"])
        out = _j(plugin.tools._handle_contract_check(
            {"contract_artifacts": ["c.yaml"], "types": ["openapi", "zod"]}
        ))
        assert len(out["validated"]) == 2
        assert out["status"] == "ok"


# ---------------------------------------------------------------------------
# research_trigger_check
# ---------------------------------------------------------------------------

class TestResearchTrigger:
    def call(self, plugin, **kw):
        base = {"reason": "tick", "completed_tasks": 0, "test_errors": 0}
        base.update(kw)
        return _j(plugin.tools._handle_research_trigger_check(base))

    def test_on_level_return_fires(self, plugin):
        out = self.call(plugin, reason="on_level_return")
        assert out["trigger"] is True
        assert "on_level_return" in out["fired_by"]

    def test_every_n_tasks_fires(self, plugin):
        out = self.call(plugin, completed_tasks=25)  # default every_n_tasks=20
        assert out["trigger"] is True

    def test_below_threshold_no_fire(self, plugin):
        out = self.call(plugin, completed_tasks=3)
        assert out["trigger"] is False

    def test_cooldown_suppresses(self, plugin):
        first = self.call(plugin, completed_tasks=25)
        assert first["trigger"] is True
        # within cooldown_tasks (default 5) of the last fire -> suppressed
        second = self.call(plugin, completed_tasks=27)
        assert second["trigger"] is False
        assert second["on_cooldown"] is True

    def test_state_persisted(self, plugin):
        self.call(plugin, completed_tasks=25)
        state_path = os.path.join(plugin.tools._spec_flow_dir(), "research_lane_state.json")
        assert os.path.exists(state_path)

    def test_disabled_lane(self, plugin, monkeypatch):
        monkeypatch.setenv("SPEC_FLOW_RESEARCH_LANE", json.dumps({"enabled": False}))
        out = self.call(plugin, reason="on_level_return")
        assert out["trigger"] is False


# ---------------------------------------------------------------------------
# policy_gate
# ---------------------------------------------------------------------------

class TestPolicyGate:
    def call(self, plugin, **kw):
        return _j(plugin.tools._handle_policy_gate(kw))

    def test_clean_node_passes(self, plugin):
        out = self.call(plugin, measurable_target=True, spend_per_action_usd=10)
        assert out["verdict"] == "pass"

    def test_missing_target_clarifies(self, plugin):
        out = self.call(plugin, measurable_target=False, spend_per_action_usd=10)
        assert out["verdict"] == "clarify"
        assert any("measurable" in c for c in out["clarifications"])

    def test_spend_over_cap_blocks(self, plugin):
        out = self.call(plugin, measurable_target=True, spend_per_action_usd=200, human_in_loop=False)
        assert out["verdict"] == "block"
        assert any("exceeds unattended cap" in b for b in out["blocks"])

    def test_spend_over_cap_with_human_passes(self, plugin):
        out = self.call(plugin, measurable_target=True, spend_per_action_usd=200, human_in_loop=True)
        assert out["verdict"] == "pass"

    def test_outreach_without_consent_blocks(self, plugin):
        out = self.call(plugin, measurable_target=True, involves_outreach=True, consent_obtained=False)
        assert out["verdict"] == "block"
        assert any("outreach" in b for b in out["blocks"])

    def test_legal_exposure_unreviewed_blocks(self, plugin):
        out = self.call(plugin, measurable_target=True, legal_exposure=True, legality_reviewed=False)
        assert out["verdict"] == "block"

    def test_block_dominates_clarify(self, plugin):
        # both an ambiguity and a violation -> block wins
        out = self.call(plugin, measurable_target=False, spend_per_action_usd=500)
        assert out["verdict"] == "block"

    def test_policy_override_via_env(self, plugin, monkeypatch):
        monkeypatch.setenv("SPEC_FLOW_POLICY", json.dumps({"max_unattended_spend_usd": 1000}))
        out = self.call(plugin, measurable_target=True, spend_per_action_usd=200)
        assert out["verdict"] == "pass"


# ---------------------------------------------------------------------------
# Seed commands — graceful without `hermes`
# ---------------------------------------------------------------------------

class TestSeedCommands:
    def test_specflow_init_creates_workspace(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        proj = plugin.tmp / "proj"
        out = _j(plugin.tools._handle_specflow_init({"project": "demo", "dir": str(proj)}))
        assert os.path.isdir(os.path.join(str(proj), "specs"))
        assert os.path.exists(os.path.join(str(proj), "constitution.md"))
        # board call degrades gracefully when hermes is absent
        assert out["board"]["ok"] is False

    def test_specflow_init_requires_args(self, plugin):
        out = _j(plugin.tools._handle_specflow_init({"project": "x"}))
        assert "error" in out

    def test_specflow_start_requires_args(self, plugin):
        out = _j(plugin.tools._handle_specflow_start({"goal": "x"}))
        assert "error" in out

    def test_specflow_start_resilient(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        out = _j(plugin.tools._handle_specflow_start({"goal": "Build X", "project": "demo"}))
        assert out["seeded"]["ok"] is False
        assert "hermes binary not found" in out["seeded"]["error"]

    def test_specflow_status_resilient(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        out = _j(plugin.tools._handle_specflow_status({"project": "demo"}))
        assert out["result"]["ok"] is False

    def test_run_kanban_missing_binary(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        res = plugin.tools._run_kanban(["show"])
        assert res["ok"] is False
        assert "not found" in res["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
