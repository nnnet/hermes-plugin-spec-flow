"""Audit rule S10.24: a late requirement routed to AMEND an owner module that
implies a genuinely NEW (method, path) BINDS that route as engine data — the
product contract, contracts/interface.json and the entry table GROW from the
binding; prose never invents a path.

v156 (p6-micro-notes): 'ALLOW REMOVING A NOTE' was routed to amend src/core.py
(trace event 57) and went to_done, yet the final interface.json has NO DELETE
route and src/app.py serves 405 for DELETE /notes (test_delete_note_removes_it
'assert 405 == 200'). Root: an amend node owns no route (`_leaf_owned_routes`
returns [] for code_target), the prose names no literal 'DELETE /notes', so no
datum anywhere carried the route — S10.13 adoption only matches the CANONICAL
name (`delete_notes`) on a DECLARED path, and with no binding printed the
coder named its handler `delete_note` (checkpoint 007). The contract never
grew; a later integrate rework then silently dropped the handler entirely.

Contract pinned here:
  * the bound route's PATH is never invented — it must be a path the amend
    target ALREADY serves per the ownership datum (`_route_owners`), minus
    fixed-body liveness paths; the METHOD comes from the requirement's own
    verbs (`_METHOD_SYNONYMS`) and must be NEW on that path; any ambiguity
    binds nothing (honest no-op);
  * a bound route enters `_declared_route_set` / interface.json with the
    single-source success status (`_route_success_status`) and the canonical
    handler name, and the entry resolver wires it;
  * the amend leaf OWNS its bound route: the route binding orders
    `def delete_notes(payload, query)` with the contracted status, so the
    handler and test-status gates hold the leaf to it;
  * S10.19 stays intact: dependency prose (no new method verb / a new PATH
    named only in prose) creates NO route.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONSTITUTION = [
    "The product entry src/app.py exposes wsgi_app (stdlib WSGI).",
    "POST /notes takes {text} and responds {id}.",
    "GET /notes responds {items: [{id, text}]} newest-first.",
    "GET /health responds 200.",
]

# the exact v156 late requirement (no literal route anywhere in the prose)
_DELETE_REQ = (
    "ALLOW REMOVING A NOTE (added by the human mid-run; binding). "
    "A reader must be able to delete a single note by its id. Extend the "
    "existing notes capability - do not add a separate store. Deleting a "
    "note then listing must no longer show it.")

_CORE_BEFORE = '''\
def post_notes(payload, query):
    return 201, {"id": 1}


def get_notes(payload, query):
    return 200, {"items": []}


def get_health(payload, query):
    return 200, {"status": "ok"}
'''


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._constitution = list(_CONSTITUTION)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "core.py").write_text(_CORE_BEFORE, encoding="utf-8")
    # the owner leaf registers its routes in the ownership datum first,
    # exactly as a live run does before any late requirement arrives
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes and GET /health"}
    assert ("POST", "/notes") in set(eng._leaf_owned_routes(core))
    return eng


def _attach_delete(eng) -> dict:
    extra = {"id": "delete_note", "title": "ALLOW REMOVING A NOTE",
             "requirement": _DELETE_REQ, "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"   # deterministic router seam
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    return extra


def test_late_req_bound_route_grows_the_contract(tmp_path):
    eng = _engine(tmp_path)
    extra = _attach_delete(eng)
    declared = set(eng._declared_route_set(eng._product_contract()))
    assert ("DELETE", "/notes") in declared, (
        "the engine routed the late req to amend the /notes owner — its "
        "bound route must GROW the declared set, else the entry 405s the "
        "behaviour forever (v156): %s" % sorted(declared))
    # never a phantom: nothing else grew
    assert not any(p not in ("/notes", "/health") for _m, p in declared), (
        "growth is method-on-owned-path only, never a new path: %s"
        % sorted(declared))
    assert extra.get("code_target") == "src/core.py"


def test_amend_leaf_owns_its_bound_route_and_gets_the_binding(tmp_path):
    eng = _engine(tmp_path)
    extra = _attach_delete(eng)
    assert set(eng._leaf_owned_routes(extra)) == {("DELETE", "/notes")}, (
        "the amend leaf must OWN its engine-bound route so the handler and "
        "test-status gates hold it to the contract")
    binding = eng._leaf_route_binding(extra)
    assert "delete_notes" in binding, (
        "the binding must order the CANONICAL handler name — v156's coder "
        "guessed `delete_note` and S10.13 adoption never matched: %r"
        % binding[:200])
    assert "SUCCESS STATUS 200" in binding, (
        "the contracted status is the single source (_route_success_status)")


def test_interface_contract_carries_the_bound_route(tmp_path):
    eng = _engine(tmp_path)
    _attach_delete(eng)
    eng._write_interface_contract()
    data = json.loads((pathlib.Path(eng.workspace.root) / "contracts"
                       / "interface.json").read_text(encoding="utf-8"))
    rows = {(r["method"], r["path"]): r for r in data["routes"]}
    row = rows.get(("DELETE", "/notes"))
    assert row, ("interface.json must carry the bound route — v156 shipped "
                 "without it: %s" % sorted(rows))
    assert row["handler"] == "delete_notes"
    assert row["success_status"] == 200


def test_entry_resolver_wires_the_bound_route(tmp_path):
    eng = _engine(tmp_path)
    _attach_delete(eng)
    # the amend coder followed the binding: canonical handler in the owner
    src = pathlib.Path(eng.workspace.root) / "src" / "core.py"
    src.write_text(_CORE_BEFORE + '''

def delete_notes(payload, query):
    return 200, {"deleted": payload.get("id")}
''', encoding="utf-8")
    mapping, unresolved = eng._resolve_route_handlers(eng._product_contract())
    got = mapping.get(("DELETE", "/notes"))
    assert got and tuple(got[:2]) == ("core", "delete_notes"), (
        "the entry table must wire the bound route to the canonical handler: "
        "mapping=%s unresolved=%s" % (mapping, unresolved))


def test_prose_path_never_invents_a_route(tmp_path):
    # S10.19 green guard: a late req whose prose names a NEW path (and no new
    # method verb for the owned path) binds nothing — no phantom growth
    eng = _engine(tmp_path)
    extra = {"id": "audit_trail", "title": "record an audit trail",
             "requirement": ("Record an audit trail page at /admin/trail "
                             "so a reader can see what happened."),
             "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    declared = set(eng._declared_route_set(eng._product_contract()))
    assert not any(p.startswith("/admin") for _m, p in declared), (
        "a path named only in prose must never enter the contract: %s"
        % sorted(declared))
    assert eng._leaf_owned_routes(extra) == []


def test_ambiguous_verbs_bind_nothing(tmp_path):
    # two NEW-method verbs at once = ambiguity, not license to guess
    eng = _engine(tmp_path)
    extra = {"id": "mixed", "title": "mixed edit",
             "requirement": ("Let a reader delete one note and also replace "
                             "the text of another note."),
             "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    declared = set(eng._declared_route_set(eng._product_contract()))
    assert declared == {("POST", "/notes"), ("GET", "/notes"),
                        ("GET", "/health")}, (
        "ambiguous verbs must bind nothing: %s" % sorted(declared))


# --- S12.11 (v162): an amend for a LITERAL new path must bind + own it --------
# v162 (2026-07-03T19-55-37): 'ADD AN ABOUT PAGE ... Serve GET /about ...' was
# routed to amend src/core.py; `_late_req_bound_route` only knows how to bind
# a NEW METHOD on a path the target already serves, so the literal GET /about
# bound NOTHING -> the amend node owned no route (`_leaf_owned_routes` == []),
# `_route_handler_modules` never recorded /about -> core, the S12.2 rework
# directive/write door had no surface to defend, the round-1 core rework
# dropped get_about, and the final plan check (tick 154) said
# 'route GET /about: no owner leaf (orphan)'. The path is NOT invented from
# prose: GET /about is already DECLARED (the contract derives from the same
# human text) — the binder only attributes ownership of a declared, unowned
# route to the amend that the engine itself routed to build it.

_ABOUT_REQ = (
    "ADD AN ABOUT PAGE (added by the human mid-run; binding). "
    "Serve GET /about as a small server-rendered HTML page describing what "
    "this little notes service is. No storage, no API change — a standalone "
    "page. Standard library only (return 200 + an HTML string). Build it "
    "AND prove GET /about returns HTML.")

_NICE_REQ = (
    "MAKE THE NOTES NICE TO READ (added by the human mid-run; binding). "
    "The notes that a reader sees should be presented in a clean, friendly, "
    "easy-to-read layout — a clear heading, a tidy list, a simple way to "
    "add one. Standard library only; improve the existing presentation "
    "rather than adding a separate one alongside it.")


def _attach_about(eng) -> dict:
    eng._standing_requirements = lambda: [("about_page", _ABOUT_REQ)]
    assert ("GET", "/about") in set(
        eng._declared_route_set(eng._product_contract())), (
        "fixture precondition: the contract already grew GET /about from "
        "the SAME human text — the binder invents nothing")
    extra = {"id": "about_page", "title": "ADD AN ABOUT PAGE",
             "requirement": _ABOUT_REQ, "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"   # deterministic router seam
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    return extra


def test_literal_new_path_amend_binds_and_owns(tmp_path):
    eng = _engine(tmp_path)
    extra = _attach_about(eng)
    assert extra.get("binds_route") == ["GET", "/about"], (
        "v162: the about amend bound NOTHING (binds_route=%r) — GET /about "
        "then had no owner, no module record, and the core rework erased "
        "get_about unopposed" % (extra.get("binds_route"),))
    assert set(eng._leaf_owned_routes(extra)) == {("GET", "/about")}, (
        "the amend leaf must OWN its bound route")
    mods = eng.__dict__.get("_route_handler_modules") or {}
    assert mods.get(("GET", "/about")) == "core", (
        "the route-surface datum must record /about into the module the "
        "amend EDITS, so the S12.2 write door defends it: %s" % mods)


def test_bound_about_enters_the_core_rework_surface(tmp_path):
    eng = _engine(tmp_path)
    _attach_about(eng)
    block = eng._module_route_binding_text("core")
    assert "GET /about" in block and "get_about" in block, (
        "the module rework directive must print the /about surface — v162's "
        "core rework prompt carried no trace of it and the weak model "
        "re-guessed the module without get_about: %r" % block[:300])


def test_final_plan_check_sees_an_owner_for_about(tmp_path):
    eng = _engine(tmp_path)
    _attach_about(eng)
    findings = [f for f in eng._plan_ownership_report()
                if "/about" in f and "orphan" in f]
    assert not findings, (
        "v162 tick 154: 'route GET /about: no owner leaf (orphan)' at the "
        "FINAL plan check — with the binding the owner must exist: %s"
        % findings)


# --- GREEN edges ---------------------------------------------------------------

def test_pure_refinement_amend_binds_nothing(tmp_path):
    # v162's красивый_вид class: no literal route, no new-method verb — a
    # presentation refinement of the owner's existing surface binds nothing
    eng = _engine(tmp_path)
    extra = {"id": "nice_view", "title": "MAKE THE NOTES NICE TO READ",
             "requirement": _NICE_REQ, "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    assert not extra.get("binds_route")
    assert eng._leaf_owned_routes(extra) == []


def test_owned_literal_route_mention_binds_nothing(tmp_path):
    # v145 protection: an amend whose prose mentions the owner's EXISTING
    # route ('the existing GET /notes list') must not claim it
    eng = _engine(tmp_path)
    extra = {"id": "reuse_notes", "title": "polish the list",
             "requirement": ("Polish the existing GET /notes list "
                             "presentation without changing the API."),
             "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    assert not extra.get("binds_route"), (
        "mentioning a route another leaf owns is a DEPENDENCY, not a claim")


def test_two_literal_unowned_routes_bind_nothing(tmp_path):
    # ambiguity is not license to guess: two literal new paths at once
    eng = _engine(tmp_path)
    req = ("ADD INFO PAGES. Serve GET /about and also GET /faq as small "
           "HTML pages.")
    eng._standing_requirements = lambda: [("info_pages", req)]
    extra = {"id": "info_pages", "title": "ADD INFO PAGES",
             "requirement": req, "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    assert not extra.get("binds_route"), (
        "two literal candidate routes = ambiguity; the binder must not pick")
