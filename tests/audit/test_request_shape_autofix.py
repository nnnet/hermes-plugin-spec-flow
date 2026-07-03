"""Audit rule S12.13 (v163): a junk payload field sent to a BODYLESS route
(contracted request shape EMPTY) is a MECHANICALLY fixable test defect — the
engine removes it itself, exactly like the S12.7 smeared-status autofix,
never round-tripping a zero-ambiguity edit through model rework.

v163 evidence (2026-07-03T21-53-39__v163__p6-micro-notes): the ONLY red over
a fully green product was "tests/test_core.py sends payload field(s)
'irrelevant' to `GET /about` outside the contracted request shape []". The
contracted shape is EMPTY (a bodyless GET) — there is exactly ONE correct
payload ({}), so the repair carries zero ambiguity: AST surgery emptying the
junk dict literal, journaled, no doctor loop.

Contract pinned here:
  * RED (pre-fix): a leaf test calling the canonical handler directly with a
    junk dict — get_about({'irrelevant': 1}, {}) — on a route whose
    contracted shape is [] is REWRITTEN in place: after the gate pass the
    call sends NO fields, the gate is clean, and a `request_shape_autofix`
    event + loop entry are journaled;
  * HONEST RED: a junk field on a route with a NON-empty contract is NOT
    autofixed — the intent is ambiguous (junk? typo of a contracted field?),
    so the gate reds with the classic finding and the file stays
    byte-identical;
  * GREEN: an already-clean call is untouched byte-identical (no autofix
    loop, no event);
  * NON-CANONICAL call shapes (the `_call("GET", "/about", {...})` string
    form) are NOT surgically edited — they stay the classic red finding
    (the engine edits only what it can edit exactly);
  * quiet mode (the S12.5 recheck) stays side-effect-free: verdict only,
    no rewrite.

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_GOAL = (
    'A tiny notes service. POST /notes accepts {"text": "..."} and stores '
    'it, returning {"id": <int>}; GET /notes returns {"items": [...]}; '
    'GET /about serves a small HTML about page.')
_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [("GET", "/about")],
    "media": {"/notes": "json", "/about": "html"},
}
_ABOUT = {"id": "about_page", "title": "About page",
          "requirement": "own GET /about"}
_NOTES = {"id": "core", "title": "Product core",
          "requirement": "own POST /notes and GET /notes"}


def _gate(tmp_path, node, code_rel, test_rel, code_body, test_body,
          quiet: bool = False):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    root = pathlib.Path(eng.workspace.root)
    for rel, body in ((code_rel, code_body), (test_rel, test_body)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    ok = eng._leaf_request_shape_gate(dict(node), str(node["id"]), 1,
                                      code_rel, test_rel, quiet=quiet)
    return eng, ok, root / test_rel


_ABOUT_CODE = """\
    def get_about(payload, query):
        return 200, "<html><body>about</body></html>"
"""

_ABOUT_JUNK_TEST = """\
    from about_page import get_about


    def test_about():
        code, body = get_about({"irrelevant": 1}, {})
        assert code == 200
"""


def _payload_dicts(text: str, fname: str) -> list:
    """First-positional dict literals of every call to `fname`."""
    out = []
    for call in ast.walk(ast.parse(text)):
        if isinstance(call, ast.Call) and (
                getattr(call.func, "id", "") == fname
                or getattr(call.func, "attr", "") == fname):
            if call.args and isinstance(call.args[0], ast.Dict):
                out.append(call.args[0])
    return out


# --- RED (v163): junk on an EMPTY contracted shape is repaired by the engine --

def test_junk_field_on_bodyless_route_is_autofixed(tmp_path):
    eng, ok, p = _gate(tmp_path, _ABOUT, "src/about_page.py",
                       "tests/test_about_page.py",
                       _ABOUT_CODE, _ABOUT_JUNK_TEST)
    text = p.read_text(encoding="utf-8")
    dicts = _payload_dicts(text, "get_about")
    assert dicts and all(not d.keys for d in dicts), (
        "the contracted shape is EMPTY — the one correct payload is {}; the "
        "engine must remove the junk field itself (the S12.7 twin), not "
        f"round-trip it through model rework; got:\n{text}")
    assert "irrelevant" not in text
    assert ok is True, (
        "after the mechanical repair the artifact satisfies the contract — "
        "the gate must be clean, not red on the pre-fix text")
    assert eng._doctor_open_causes() == [], (
        "a mechanically repaired defect must not open a doctor cause — the "
        "v163 eternal-red fuel")
    fixes = [l for l in eng.loops
             if l.get("type") == "request-shape-autofix"]
    assert fixes and "irrelevant" in fixes[0]["detail"] \
        and "test_about_page.py" in fixes[0]["detail"], (
        f"the autofix must be journaled naming file and field, got {fixes}")
    ev = [e for e in eng.events
          if getattr(e, "gate", "") == "request_shape_autofix"]
    assert ev, "the autofix must be a visible request_shape_autofix event"
    compile(text, str(p), "exec")       # the repaired file is valid python


def test_autofixed_file_keeps_other_lines_intact(tmp_path):
    _eng, _ok, p = _gate(tmp_path, _ABOUT, "src/about_page.py",
                         "tests/test_about_page.py",
                         _ABOUT_CODE, _ABOUT_JUNK_TEST)
    text = p.read_text(encoding="utf-8")
    assert "from about_page import get_about" in text
    assert "assert code == 200" in text, (
        "the surgery touches ONLY the junk dict literal — every other line "
        f"survives byte-identical; got:\n{text}")


# --- HONEST RED: ambiguity is never autofixed ----------------------------------

def test_junk_field_on_non_empty_contract_is_not_autofixed(tmp_path):
    src = """\
        from core import post_notes


        def test_post():
            code, body = post_notes({"text": "hi", "junk": 1}, {})
            assert code == 201
    """
    eng, ok, p = _gate(tmp_path, _NOTES, "src/core.py",
                       "tests/test_core.py", """\
        def post_notes(payload, query):
            return 201, {"id": 1}
    """, src)
    assert ok is False, (
        "POST /notes contracts ['text'] — a junk extra field is AMBIGUOUS "
        "(junk? typo of a contracted field?), no mechanical answer exists; "
        "the gate must red honestly, never guess an edit")
    assert p.read_text(encoding="utf-8") == textwrap.dedent(src), (
        "an ambiguous defect leaves the file byte-identical")
    assert not [l for l in eng.loops
                if l.get("type") == "request-shape-autofix"]
    assert [l for l in eng.loops
            if l.get("type") == "request-shape-mismatch"], (
        "the classic red finding must still be journaled")


def test_non_canonical_call_shape_is_not_surgically_edited(tmp_path):
    # the engine edits only what it can edit exactly: the string-route
    # `_call("GET", "/about", {...})` form stays a classic red finding
    src = """\
        def _call(method, path, payload):
            return 200, ""


        def test_about():
            code, body = _call("GET", "/about", {"irrelevant": 1})
            assert code == 200
    """
    eng, ok, p = _gate(tmp_path, _ABOUT, "src/about_page.py",
                       "tests/test_about_page.py", _ABOUT_CODE, src)
    assert ok is False
    assert p.read_text(encoding="utf-8") == textwrap.dedent(src)
    assert not [l for l in eng.loops
                if l.get("type") == "request-shape-autofix"]


# --- GREEN: clean artifacts untouched, quiet mode side-effect-free -------------

def test_already_clean_call_is_untouched_byte_identical(tmp_path):
    src = """\
        from about_page import get_about


        def test_about():
            code, body = get_about({}, {})
            assert code == 200
    """
    eng, ok, p = _gate(tmp_path, _ABOUT, "src/about_page.py",
                       "tests/test_about_page.py", _ABOUT_CODE, src)
    assert ok is True
    assert p.read_text(encoding="utf-8") == textwrap.dedent(src), (
        "a clean file must never be rewritten — byte-identical")
    assert not [l for l in eng.loops
                if l.get("type") == "request-shape-autofix"]
    assert not [e for e in eng.events
                if getattr(e, "gate", "") == "request_shape_autofix"]


def test_quiet_recheck_never_rewrites(tmp_path):
    eng, ok, p = _gate(tmp_path, _ABOUT, "src/about_page.py",
                       "tests/test_about_page.py",
                       _ABOUT_CODE, _ABOUT_JUNK_TEST, quiet=True)
    assert ok is False, (
        "quiet mode judges the CURRENT artifact — the junk field is present")
    assert "irrelevant" in p.read_text(encoding="utf-8"), (
        "the S12.5 recheck is side-effect-free: verdict only, no rewrite")
    assert eng.loops == [] and eng._doctor_open_causes() == []
