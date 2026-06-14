"""Battle test — contract_test against a running API via specmatic (roadmap D4).

contract_check verifies a contract against the CODE statically; D4 adds the live
half — run the OpenAPI contract as tests against a started service (specmatic).
The wrapper is offline-safe: without specmatic it reports status='skipped'. Here
the specmatic command is pointed at a stub script so pass/fail parsing is
verified WITHOUT installing specmatic.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# a stub that emulates specmatic: exit 0 on a "good" base url, 1 otherwise
STUB = """#!/usr/bin/env python3
import sys
argv = sys.argv[1:]
base = next((a.split("=", 1)[1] for a in argv if a.startswith("--testBaseURL=")), "")
if "good" in base:
    print("Tests passed: 4, Failures: 0")
    sys.exit(0)
print("Tests passed: 2, Failures: 2")
sys.exit(1)
"""


@pytest.fixture
def stub_specmatic(tmp_path, plugin):
    script = tmp_path / "specmatic_stub.py"
    script.write_text(STUB, encoding="utf-8")
    os.chmod(script, 0o755)
    contract = tmp_path / "api.openapi.yaml"
    contract.write_text("openapi: 3.0.0\n", encoding="utf-8")
    plugin.tools.SPECMATIC_CMD = ["python3", str(script),
                                  "{contract}", "--testBaseURL={base_url}"]
    return contract


# ─── pass / fail against a running service ────────────────────────────


def test_passes_against_good_service(plugin, stub_specmatic):
    out = json.loads(plugin.tools._handle_contract_test(
        {"contract": str(stub_specmatic), "base_url": "http://good.local"}))
    assert out["status"] == "pass"
    assert out["available"] is True
    assert "Failures: 0" in out["report"]


def test_fails_against_bad_service(plugin, stub_specmatic):
    out = json.loads(plugin.tools._handle_contract_test(
        {"contract": str(stub_specmatic), "base_url": "http://bad.local"}))
    assert out["status"] == "fail"
    assert out["exit_code"] == 1


# ─── graceful degradation without specmatic ───────────────────────────


def test_skipped_when_specmatic_absent(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.tools, "SPECMATIC_CMD",
                        ["specmatic-not-installed-xyz", "test", "{contract}", "--testBaseURL={base_url}"])
    out = json.loads(plugin.tools._handle_contract_test(
        {"contract": "api.yaml", "base_url": "http://x"}))
    assert out["status"] == "skipped"
    assert out["available"] is False


def test_strict_turns_skip_into_fail(plugin, monkeypatch):
    monkeypatch.setattr(plugin.tools, "SPECMATIC_CMD",
                        ["specmatic-not-installed-xyz", "test", "{contract}", "--testBaseURL={base_url}"])
    out = json.loads(plugin.tools._handle_contract_test(
        {"contract": "api.yaml", "base_url": "http://x", "strict": True}))
    assert out["status"] == "fail"


def test_requires_contract_and_url(plugin):
    out = json.loads(plugin.tools._handle_contract_test({"contract": "x"}))
    assert "error" in out


def test_tool_registered(plugin):
    assert "contract_test" in plugin.reg.tools
