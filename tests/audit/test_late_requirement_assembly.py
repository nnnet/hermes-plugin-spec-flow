"""Audit rule (investigator F10, v150): every late requirement reaches its
contribution to the ASSEMBLY, or a node goes honestly red — never a silent drop.

v150 lost two late requirements between green leaves and the assembled entry:
  * красивый_вид edited the notes owner in place, adding ('GET', '/') to the
    module's OWN dispatch table; the neutralizer replaced the owner's WSGI
    callable with a delegator to the declared entry, which never wired GET /
    — 4 suite tests red forever;
  * note_search's q-filter lived in the wired get_notes handler, but the
    synthesized router passed raw parse_qs LISTS while the leaf ABI contracts
    scalar query values — the filter never matched (1 test red forever).

Contract enforced here:
  * the resolver/synthesized entry ADOPT routes served only by a module's own
    dispatch table (extending, never overriding, the declared set);
  * the synthesized router hands handlers SCALAR query values;
  * consistency.check_module_routes_reach_entry flags a table route the entry
    dropped (known-answer red on the stored v150 artifacts);
  * journal invariant: a late requirement that is picked up but never reaches
    to_done nor an honest red verdict is flagged.

Deterministic: engine unit calls, artifact scans, synthetic journals — no LLM.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import spec_flow_runner as sfr  # noqa: E402
import consistency  # noqa: E402
import journal_invariants  # noqa: E402

V150_RUN = (pathlib.Path(__file__).resolve().parents[1] / "runs-out"
            / "2026-07-02T20-18-57__v150__p6-micro-notes")

needs_v150 = pytest.mark.skipif(
    not V150_RUN.is_dir(), reason="v150 run dir not present on this checkout")

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes"},
    "routes": [],
}

# a feature module whose OWN dispatch table serves a route the declared
# contract never mentions (the v150 красивый_вид shape after the in-place edit)
_OWNER_MODULE = '''\
def get_root(payload, query):
    return (200, "<html><body>notes</body></html>")


def post_notes(payload, query):
    return (201, {"id": 1})


def get_notes(payload, query):
    return (200, {"q": query.get("q")})


_ROUTES = {("GET", "/"): get_root, ("POST", "/notes"): post_notes,
           ("GET", "/notes"): get_notes}
'''


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "owner_mod.py").write_text(_OWNER_MODULE, encoding="utf-8")
    return eng


def test_resolver_adopts_a_module_table_route(tmp_path):
    eng = _engine(tmp_path)
    mapping, _unresolved = eng._resolve_route_handlers(_CONTRACT)
    assert ("GET", "/") in mapping, (
        "a route served only by the module's own dispatch table must be "
        "adopted into the assembly, not silently dropped (v150 GET /)")
    assert mapping[("GET", "/")][:2] == ("owner_mod", "get_root")


def test_synthesized_entry_wires_the_adopted_route(tmp_path):
    eng = _engine(tmp_path)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code and "('GET', '/')" in code, (
        "the deterministic entry must dispatch the adopted route")


def _wsgi_call(code, src_dir, method, path, query_string=""):
    sys.path.insert(0, str(src_dir))
    added = set()
    try:
        ns: dict = {}
        exec(compile(code, "app.py", "exec"), ns)  # noqa: S102 — audit fixture
        added = {m for m in sys.modules if m == "owner_mod"}
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
                   "QUERY_STRING": query_string, "CONTENT_LENGTH": "0",
                   "wsgi.input": io.BytesIO(b"")}
        cap: dict = {}

        def start_response(status, headers):
            cap["status"] = status

        raw = b"".join(ns["wsgi_app"](environ, start_response))
        return cap.get("status", ""), raw
    finally:
        sys.path.remove(str(src_dir))
        for m in added:
            sys.modules.pop(m, None)


def test_synthesized_router_passes_scalar_query_values(tmp_path):
    eng = _engine(tmp_path)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    status, raw = _wsgi_call(code, pathlib.Path(eng.workspace.root) / "src",
                             "GET", "/notes", query_string="q=apple")
    assert status.startswith("200"), (status, raw)
    assert json.loads(raw)["q"] == "apple", (
        "the router must hand handlers SCALAR query values — raw parse_qs "
        "lists silently broke every filter (v150 note_search): %r" % raw)


@needs_v150
def test_v150_dropped_table_route_is_flagged():
    findings = consistency.audit(V150_RUN)
    hits = [f for f in findings if f.kind == "module_route_not_wired_in_entry"]
    assert hits, "v150's dropped GET / (красивый_вид) must be flagged"
    assert any("GET /" in f.message for f in hits)


def test_entry_serving_every_table_route_is_clean(tmp_path):
    run = tmp_path / "run"
    src = run / "workspace" / "src"
    src.mkdir(parents=True)
    (run / "tree.json").write_text(json.dumps({"id": "L0"}), encoding="utf-8")
    (src / "core.py").write_text(
        "def get_root(payload, query):\n    return (200, 'ok')\n\n"
        "_ROUTES = {('GET', '/'): get_root}\n", encoding="utf-8")
    (src / "app.py").write_text(
        '"""Product entry — generated deterministically by the spec-flow '
        'engine."""\n'
        "_ROUTES = {('GET', '/'): ('pq', None)}\n", encoding="utf-8")
    assert consistency.check_module_routes_reach_entry(run, {"id": "L0"}) == []


def _late_req_events(tail):
    return [
        {"task": "web_ui", "phase": "decompose",
         "action": "late requirement materialized at root level",
         "detail": "", "verdict": "ATTACHED", "level": 1},
        *tail,
    ]


def test_journal_flags_a_late_requirement_that_never_closes():
    events = _late_req_events([
        {"task": "web_ui", "phase": "review", "action": "spec lint clean",
         "detail": "", "verdict": "PASS", "level": 1},
    ])
    findings = journal_invariants.check_late_requirement_ownership(
        events, "trace.jsonl")
    assert any(f.kind == "late_requirement_unclosed" for f in findings), (
        "a late requirement touched but never closed (no to_done, no honest "
        "red) must be flagged")


def test_journal_accepts_a_closed_late_requirement():
    done = _late_req_events([
        {"task": "web_ui", "phase": "lifecycle", "action": "to_done",
         "detail": "", "verdict": "", "level": 2},
    ])
    red = _late_req_events([
        {"task": "web_ui", "phase": "review", "action": "card gate",
         "detail": "", "verdict": "FAIL", "level": 1},
    ])
    for events in (done, red):
        assert journal_invariants.check_late_requirement_ownership(
            events, "trace.jsonl") == []
