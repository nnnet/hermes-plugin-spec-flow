"""Audit rule S10.23: the ENTRY leaf's tests exercise EVERY declared route —
they are held to the CONTRACTED success status of each one, never exempted.

v156 (p6-micro-notes): the assembly leaf (product_entry, code_target =
src/app.py) shipped tests/test_app.py asserting ``code == 200`` for POST
/notes while the single-source contract (`_route_success_status`, mirrored in
contracts/interface.json) says 201 — and the assembled product correctly
returned 201. The leaf test-status gate (S10.4/S10.6) was a NO-OP for the
entry leaf because `_leaf_owned_routes` returns [] for ANY node carrying
``code_target`` (the amend exemption), so the wrong TEST survived to
assembly, where the doctor read "assert 201 == 200", blamed the CORE module
and reworked the wrong artifact three times (trace events 139-143), losing a
delivered handler on the way.

Contract pinned here:
  * an entry-leaf test asserting a NON-contracted success status on a
    declared route reds AT THE LEAF, naming the exact expected value;
  * an entry-leaf test asserting the contracted statuses passes;
  * a non-entry amend leaf (code_target != declared entry, no bound route)
    keeps its existing exemption — S10.4/S10.6 behaviour unchanged.

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}

# the exact v156 shape of the assembly node (see _assembly_node)
_ENTRY_NODE = {"id": "product_entry",
               "title": "Assemble product entry (src/app.py)",
               "code_target": "src/app.py", "_no_amend": True}


def _gate(tmp_path, node, test_body: str) -> "tuple[bool, list]":
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    rel = "tests/test_app.py"
    p = pathlib.Path(eng.workspace.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(test_body), encoding="utf-8")
    ok = eng._leaf_test_status_gate(node, str(node.get("id")), 1, rel)
    return ok, [l for l in eng.loops if l["type"] == "test-status-mismatch"]


def test_entry_test_wrong_status_on_declared_route_is_red(tmp_path):
    # v156: tests/test_app.py asserted 200 for POST /notes (contract: 201)
    ok, loops = _gate(tmp_path, dict(_ENTRY_NODE), """\
        def _call(method, path):
            return 200


        def test_post_notes_returns_id():
            code = _call("POST", "/notes")
            assert code == 200
    """)
    assert not ok and loops, (
        "the entry leaf's test asserting 200 for POST /notes (contract: 201) "
        "must red AT THE LEAF — v156 let it reach assembly, where the doctor "
        "blamed and reworked the wrong module")
    assert "201" in loops[0]["detail"], (
        "the finding must name the exact contracted status so the rework "
        "fixes the assertion, not guesses: %s" % loops[0]["detail"])


def test_entry_test_smeared_success_never_survives(tmp_path):
    # S12.7 (v161): this is the LITERAL v160/v161 artifact — tests/test_app.py
    # smearing POST /notes over (200, 201) survived model rework twice. The
    # contracted 201 is IN the set, so the engine repairs it mechanically;
    # the gate is clean over the repaired file and the fix is journaled.
    ok, loops = _gate(tmp_path, dict(_ENTRY_NODE), """\
        def _call(method, path):
            return 201


        def test_post_notes_created():
            code = _call("POST", "/notes")
            assert code in (200, 201)
    """)
    assert ok and not loops, (
        "the fixable smear on the entry leaf is repaired in place — never "
        "round-tripped through a model that failed the exact feedback twice")


def test_entry_test_contracted_statuses_pass(tmp_path):
    ok, loops = _gate(tmp_path, dict(_ENTRY_NODE), """\
        def _call(method, path):
            return 200


        def test_post_notes_created():
            code = _call("POST", "/notes")
            assert code == 201


        def test_get_notes_ok():
            code = _call("GET", "/notes")
            assert code == 200


        def test_health_ok():
            code = _call("GET", "/health")
            assert code == 200


        def test_unknown_404():
            code = _call("GET", "/nope")
            assert code == 404
    """)
    assert ok and not loops, (
        "the entry leaf legitimately exercises EVERY declared route — "
        "contracted assertions must pass: %s" % loops)


def test_non_entry_amend_leaf_exemption_unchanged(tmp_path):
    # S10.4/S10.6 green case: an amend leaf editing a FEATURE module (not the
    # entry) with no engine-bound route still owns nothing — gate stays inert
    amend = {"id": "beautify", "title": "make notes pretty",
             "code_target": "src/core.py",
             "requirement": "polish the POST /notes output"}
    ok, loops = _gate(tmp_path, amend, """\
        def _call(method, path):
            return 200


        def test_post_notes_pretty():
            code = _call("POST", "/notes")
            assert code == 200
    """)
    assert ok and not loops, (
        "a non-entry amend leaf keeps the code_target exemption — "
        "the entry rule must not leak onto feature amends")
