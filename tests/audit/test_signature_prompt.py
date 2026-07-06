"""Audit stage S43 (node Q5, plan 2026-07-06T20-15 systemic-design-fixes):
the typed INPUT->OUTPUT signature pilot. The dspy.Signature idea — give a weak
model an explicit CONTRACT OF GENERATION (typed inputs it is handed, typed
outputs it must produce) — delivered DETERMINISTICALLY from the IR carrier, with
no heavyweight dspy dependency. It is an opt-in pilot: EMPTY by default so no
live prompt changes until it is measured against the plain Q1 carrier.

  * S43.1 — default OFF: `_signature_block` is empty unless
    SPEC_FLOW_SIGNATURE_PROMPT is set (a pilot changes nothing until enabled).
  * S43.2 — enabled: the block carries a typed INPUTS section (the machine
    contract, dependencies, acceptance) and a typed OUTPUT section (code +
    tests + the exact symbols to expose), derived from the IR carrier.
  * S43.3 — enabled: in the assembled coder prompt the signature PRECEDES the
    machine carrier (the generation contract heads the task).
  * S43.4 — zero new dependency: role_worker never imports dspy; the pilot is
    pure deterministic rendering.

Deterministic: env-toggled string assembly, no LLM, no network, no new dep.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402
from harness import role_worker as rw  # noqa: E402

_NODE = {
    "id": "kinds",
    "title": "kind store",
    "openapi": {"openapi": "3.1.0",
                "paths": {"/kinds": {"get": {"responses": {"200": {}}}}}},
    "behavior": "Feature: kinds\n  Scenario: list\n    When listed\n"
                "    Then kinds return",
    "symbols": {"exposes": ["list_kinds()"], "consumes": ["db.connect()"]},
    "env": ["KINDS_DB"],
}
_FRAG = {"schema": {"type": "object", "properties": {"k": {"type": "string"}}}}


def _ctx():
    return {
        "title": _NODE["title"],
        "carrier": sfr.machine_carrier_of(_NODE, _FRAG),
        "acceptance": ["list returns 200", "empty store returns []"],
    }


def _set(flag):
    if flag:
        os.environ["SPEC_FLOW_SIGNATURE_PROMPT"] = "1"
    else:
        os.environ.pop("SPEC_FLOW_SIGNATURE_PROMPT", None)


# ── S43.1: default OFF ──────────────────────────────────────────────────────

def test_signature_empty_by_default():
    _set(False)
    assert rw._signature_block(_ctx(), "kinds") == "", \
        "the pilot must be empty unless explicitly enabled"


# ── S43.2: enabled carries typed input/output ───────────────────────────────

def test_signature_typed_when_enabled():
    _set(True)
    try:
        block = rw._signature_block(_ctx(), "kinds")
    finally:
        _set(False)
    assert "SIGNATURE" in block, "must announce a typed signature"
    assert "INPUTS:" in block and "OUTPUT:" in block, "typed sections required"
    assert "contract" in block, "the machine contract is an input"
    assert "acceptance" in block, "acceptance criteria are an input"
    assert "dependencies" in block, "consumed sibling symbols are an input"
    assert "src/kinds.py" in block, "the output code module is typed"
    assert "tests/test_kinds.py" in block, "the output test module is typed"
    assert "list_kinds()" in block, "the exact symbol to expose is an output"


# ── S43.3: precedes the carrier in the assembled prompt ─────────────────────

def test_signature_precedes_carrier(tmp_path):
    ws = tmp_path
    (ws / "specs").mkdir()
    (ws / "specs" / "kinds.md").write_text("prose", encoding="utf-8")
    ctx = dict(_ctx(), spec="specs/kinds.md")
    _set(True)
    try:
        prompt = rw._coder_chat_prompt(ctx, str(ws), "kinds", "kinds")
    finally:
        _set(False)
    assert "TASK SIGNATURE" in prompt and "MACHINE CONTRACT" in prompt
    assert prompt.index("TASK SIGNATURE") < prompt.index("MACHINE CONTRACT"), \
        "the generation signature must head the task, before the carrier"


# ── S43.4: no heavyweight dependency ────────────────────────────────────────

def test_no_dspy_dependency():
    src = pathlib.Path(rw.__file__).read_text(encoding="utf-8")
    assert "import dspy" not in src and "from dspy" not in src, \
        "the pilot is pure deterministic rendering — dspy stays out of deps"
