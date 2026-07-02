"""Audit rule (revision P4, v150): a swallowed exception must leave an
attributable trace — a detail-level event, a loop record or a log record —
never a zero-evidence `except Exception: pass`.

Three deaf sites in v150 (spec_flow_runner.py ~1714 / ~2768 / ~4429):
  * ``Workspace.contract`` — a failed contract copy vanished silently;
  * ``_doctor_resolve`` — a failure while CLOSING a doctor cause vanished,
    leaving ``last_cause`` set to falsely block completion with no evidence;
  * ``_try_synthesize_entry`` — an unexpected resolver/synthesiser crash was
    silently converted into 'no entry' (and the rival-promote twin likewise).

The flow is unchanged (same return values); only attribution is added.
Deterministic: engine unit calls with induced failures, no LLM.
"""
from __future__ import annotations

import logging
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def test_workspace_contract_copy_failure_is_logged(tmp_path, caplog):
    ws = sfr.Workspace(root=str(tmp_path / "wk"), enabled=True)
    with caplog.at_level(logging.WARNING, logger="spec_flow.workspace"):
        rel = ws.contract(str(tmp_path / "no-such-source.yaml"), "api.yaml")
    assert rel == "contracts/api.yaml"  # the flow is unchanged
    assert any("contract" in r.message for r in caplog.records), (
        "a failed contract copy must leave a log record, not vanish")


def test_doctor_resolve_failure_is_attributed(tmp_path):
    eng = _engine(tmp_path)
    eng._doctor = types.SimpleNamespace(enabled=True)
    eng._doctor_states = {"n1": {"last_cause": "weak_implementer"}}

    def _boom(*a, **k):
        raise RuntimeError("emit path down")

    eng.emit = _boom
    eng._doctor_resolve({"id": "n1"}, "n1", "handler_gate")  # must not raise
    errs = [l for l in eng.loops
            if l.get("type") == "doctor" and l.get("outcome") == "error"]
    assert errs and "emit path down" in errs[0].get("detail", ""), (
        "a failure while closing a doctor cause must be recorded in loops")


def test_entry_synthesis_crash_emits_a_detail_event(tmp_path):
    eng = _engine(tmp_path)
    eng._product_contract = lambda: {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"json_roundtrip": "/notes"}, "routes": []}

    def _boom(contract):
        raise RuntimeError("resolver exploded")

    eng._resolve_route_handlers = _boom
    assert eng._try_synthesize_entry() is False  # the flow is unchanged
    hits = [e for e in eng.events if "resolver exploded" in (e.detail or "")]
    assert hits, ("an unexpected resolver crash must emit an attributable "
                  "event, not silently become 'no entry'")
