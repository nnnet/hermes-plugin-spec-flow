"""Battle tests — contract drift via a REAL validator.

Instead of a true/false stub, contract_check is pointed at ``openapi_diff.py``
(a small but real OpenAPI-vs-implementation validator) and run over real
contract + code fixtures: a clean implementation, a type mismatch and a
missing endpoint. Also covers the subtree parallel mode used by spec-integrate.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import simulator as sim  # noqa: E402

CONTRACTS = pathlib.Path(__file__).resolve().parent.parent / "contracts"
OPENAPI = CONTRACTS / "url_shortener.openapi.yaml"
CART = CONTRACTS / "cart.openapi.yaml"
CODE_CLEAN = CONTRACTS / "code_clean.json"
CODE_TYPE = CONTRACTS / "code_type_mismatch.json"
CODE_MISSING = CONTRACTS / "code_missing_endpoint.json"


@pytest.fixture
def real_validator(plugin, monkeypatch):
    """Wire contract_check's OpenAPI validator to the real openapi_diff.py."""
    cmd = ["python3", str(sim.OPENAPI_DIFF), "{contract}", "{code}"]
    monkeypatch.setitem(plugin.tools.CONTRACT_VALIDATORS, "openapi", cmd)
    return plugin


def _check(plugin, contracts, code, **kw):
    args = {"contract_artifacts": [str(c) for c in contracts],
            "changed_files": [str(code)], "types": ["openapi"]}
    args.update(kw)
    return json.loads(plugin.tools._handle_contract_check(args))


class TestContractDrift:
    def test_clean_implementation_passes(self, real_validator):
        out = _check(real_validator, [OPENAPI], CODE_CLEAN)
        assert out["status"] == "ok"
        assert out["validated"] and not out["drift"]

    def test_type_mismatch_is_drift(self, real_validator):
        out = _check(real_validator, [OPENAPI], CODE_TYPE)
        assert out["status"] == "drift"
        detail = out["drift"][0]["detail"]
        assert "type_mismatch" in detail and "\"id\"" in detail

    def test_missing_endpoint_is_drift(self, real_validator):
        out = _check(real_validator, [OPENAPI], CODE_MISSING)
        assert out["status"] == "drift"
        assert "missing_endpoint" in out["drift"][0]["detail"]

    def test_subtree_parallel_catches_sibling_drift(self, real_validator):
        # spec-integrate union: validate the clean code against TWO contracts;
        # the cart contract is not satisfied by the url-shortener code -> drift.
        out = _check(real_validator, [OPENAPI, CART], CODE_CLEAN)
        assert out["status"] == "drift"
        # the url-shortener contract still validates; the cart one drifts
        assert len(out["validated"]) == 1
        assert len(out["drift"]) == 1

    def test_strict_mode_with_missing_binary(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.tools.shutil, "which", lambda b: None)
        out = _check(plugin, [OPENAPI], CODE_CLEAN, strict=True)
        assert out["status"] == "drift"  # unavailable validator fails in strict
