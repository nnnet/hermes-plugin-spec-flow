"""Audit stage S49 (node Q9 completion, plan 2026-07-06T20-15): an AMEND node is
not hollow. Root cause of the persistent v170/v171/v172 hollow on req_a54f9144
('make the notes nice to read'): the amend router DID fire — the trace shows
'late requirement routed to EDIT existing surface' — so the requirement folds
into an owner module (code_target set), carrying NO carrier of its own by design
(its carrier is the owner's). But `code_target` is a runtime attribute never
serialized to the IR, so validate_ir saw a carrier-less node and the Q2 hollow
check (S38) flagged it. The engine already exempts amend nodes from the card and
ownership gates (code_target); the IR must carry the same marker so the hollow
check exempts them too — exactly as a branch or the assembly entry is exempt.

  * S49.1 — a node with `code_target` (edits an existing owner in place) is NOT
    hollow; the same node without it is (the verdict hinges on the marker).
  * S49.2 — `code_target` is an allowed IR node field and build_ir serialises it
    from the tree node.

Deterministic: classifier + schema/source assertion, no run.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_ir  # noqa: E402


# ── S49.1: amend node exempt from hollow ────────────────────────────────────

def test_amend_node_not_hollow():
    node = {"files": ["src/web_ui.py"], "code_target": "src/web_ui.py"}
    assert spec_ir._hollow_node_reason("req_make_nice", node) is None, \
        "an amend node edits an owner in place — carrier-less by design, not hollow"


def test_same_node_without_code_target_is_hollow():
    node = {"files": ["src/web_ui.py"]}
    assert spec_ir._hollow_node_reason("req_make_nice", node) is not None, \
        "without the amend marker the carrier-less node is genuinely hollow"


# ── S49.2: code_target is a serialisable IR field ───────────────────────────

def test_code_target_is_allowed_ir_field():
    assert "code_target" in spec_ir._NODE_KEYS, \
        "code_target must be an allowed IR node key"
    # a node carrying code_target must validate against the jsonschema
    ir = {"format": spec_ir.IR_FORMAT, "product": {},
          "nodes": {"req_x": {"files": ["src/app.py"],
                              "code_target": "src/app.py"}}}
    errs = spec_ir.jsonschema_errors(ir) if hasattr(spec_ir, "jsonschema_errors") \
        else []
    assert not errs, "code_target must be accepted by the IR jsonschema: %r" % errs


def test_build_ir_serialises_code_target():
    src = pathlib.Path(spec_ir.__file__).read_text(encoding="utf-8")
    assert 'code_target' in src and 'entry["code_target"]' in src, \
        "build_ir must serialise code_target from the tree node into the IR"
