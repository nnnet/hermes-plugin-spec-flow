"""Audit STAGE 10 — route-ownership DERIVATION precision (the v154
postmortem, pinned as S10.19).

v154 (p6-micro-notes) redded honestly at root integrate with 'duplicate route
ownership' (POST /notes, GET /notes: core + web_ui) — but the rival was
CREATED BY THE ENGINE ITSELF: `_leaf_owned_routes` matched web_ui's
DEPENDENCY prose ("reusing the existing notes storage and /notes logic") and
the inherited parent-goal quotes, so `_leaf_route_binding` ORDERED web_ui to
implement post_notes/get_notes next to core's (workspace/specs/web_ui.md
lines 26-27 in the v154 run dir). Rules pinned here:

- S10.19a a node that DECLARES `exposes` (C2 typed edges) owns exactly the
  declared routes — prose is never consulted for it.
- S10.19b the prose fallback matches ONLY the node's OWN claim text —
  inherited context lines ('Traces-to', 'Goal:' parent-goal quotes) never
  register ownership.
- S10.19c a prose-matched route ALREADY recorded to another leaf in
  `_route_owners` is a DEPENDENCY, not a claim: it is excluded from `owned`
  (first-owner-wins, deterministic by the datum) and journaled as
  'route dependency (owned by <leaf>)'.
"""
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402


def _engine(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [("GET", "/ui")],
    }
    return e


def _owners(e):
    return e.__dict__.get("_route_owners") or {}


# the exact v154 shape: the human requirement names /notes as a DEPENDENCY
_V154_WEB_UI_REQ = (
    "MINIMAL WEB INTERFACE (added by the human mid-run; binding).\n\n"
    "Serve a server-rendered HTML page, standard library only, as one feature"
    " leaf (src/web_ui.py) reusing the existing notes storage and /notes"
    " logic:\n\n- GET /ui  — an HTML page that lists all notes (newest first)")


# ── S10.19c dependency prose never claims a foreign route ────────────────────

def test_dependency_prose_never_claims_owned_route(tmp_path):
    e = _engine(tmp_path)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert set(e._leaf_owned_routes(core)) == {("POST", "/notes"),
                                               ("GET", "/notes")}
    web_ui = {"id": "web_ui", "title": "minimal web interface",
              "requirement": _V154_WEB_UI_REQ, "_late_req": True}
    owned = e._leaf_owned_routes(web_ui)
    assert owned == [("GET", "/ui")], (
        "'reusing the existing … /notes logic' is a DEPENDENCY reference —"
        " matching it made the engine order rival post_notes/get_notes"
        f" handlers in v154; got owned={owned!r}")
    assert _owners(e).get(("POST", "/notes")) == {"core"}, (
        "the datum must keep core as the SOLE owner — web_ui must not be"
        " recorded on a route it merely depends on")


def test_dependency_binding_orders_no_rival_handler(tmp_path):
    # v154 specs/web_ui.md lines 26-27: the ENGINE-declared binding ordered
    # `def post_notes` / `def get_notes` in the web_ui leaf — the rival
    # duplicate S10.17 later redded was engine-made
    e = _engine(tmp_path)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(core)
    web_ui = {"id": "web_ui", "title": "minimal web interface",
              "requirement": _V154_WEB_UI_REQ, "_late_req": True}
    binding = e._leaf_route_binding(web_ui)
    assert "get_ui" in binding, "the leaf's OWN new route must stay contracted"
    assert "post_notes" not in binding and "get_notes" not in binding, (
        "the binding must never order a handler for a route another leaf"
        f" owns (v154 rival handlers) — got: {binding!r}")


def test_dependency_exclusion_is_journaled(tmp_path):
    # the datum makes the rule deterministic; the journal makes it attributable
    e = _engine(tmp_path)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(core)
    web_ui = {"id": "web_ui", "title": "minimal web interface",
              "requirement": _V154_WEB_UI_REQ, "_late_req": True}
    e._leaf_owned_routes(web_ui)
    dump = "\n".join(repr(ev) for ev in e.events)
    assert "route dependency (owned by core)" in dump, (
        "excluding a dependency-matched route must leave an attributable"
        f" journal event naming the owner — events: {dump[-400:]!r}")


# ── S10.19a declared exposes are the ONLY source when present ────────────────

def test_exposes_declared_ownership_ignores_prose(tmp_path):
    e = _engine(tmp_path)                    # fresh datum: /notes is UNOWNED
    node = {"id": "web_ui", "title": "web ui",
            "exposes": ["get_ui(payload, query)"],
            "requirement": "GET /ui page; reuse POST /notes and GET /notes"}
    owned = e._leaf_owned_routes(node)
    assert owned == [("GET", "/ui")], (
        "a node carrying typed `exposes` owns EXACTLY the declared routes —"
        f" prose must not add /notes claims; got {owned!r}")


def test_exposes_declared_foreign_claim_is_kept(tmp_path):
    # a DECLARED claim is authoritative — a genuine duplicate must stay in
    # the datum so S10.17/S10.18 red on it (never silently dropped)
    e = _engine(tmp_path)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(core)
    rival = {"id": "web_ui", "title": "web ui",
             "exposes": ["post_notes(payload, query)",
                         "get_ui(payload, query)"]}
    owned = e._leaf_owned_routes(rival)
    assert ("POST", "/notes") in owned, (
        "a typed-edge claim of a foreign route is a REAL duplicate the gates"
        " must see — the dependency rule applies to prose only")
    assert _owners(e).get(("POST", "/notes")) == {"core", "web_ui"}


# ── S10.19b inherited context lines never register ownership ─────────────────

def test_inherited_context_lines_never_claim(tmp_path):
    e = _engine(tmp_path)                    # fresh datum: /notes is UNOWNED
    node = {"id": "web_ui", "title": "web ui",
            "requirement": "serve the HTML page at GET /ui",
            "spec_markdown": ("- **Traces-to:** A tiny notes service:"
                              " POST /notes stores; GET /notes lists.\n"
                              "Goal: POST /notes accepts {text}; GET /notes"
                              " returns the items.\n"
                              "## Scope\nRender the /ui page.")}
    owned = e._leaf_owned_routes(node)
    assert owned == [("GET", "/ui")], (
        "'Traces-to'/'Goal:' lines QUOTE the parent goal — matching them"
        f" attributed the whole product to one leaf; got {owned!r}")


# ── GREEN: legitimate claims stay untouched ──────────────────────────────────

def test_own_new_route_prose_still_owns(tmp_path):
    e = _engine(tmp_path)
    leaf = {"id": "web_ui", "title": "web ui",
            "requirement": "serve the HTML page at GET /ui"}
    assert e._leaf_owned_routes(leaf) == [("GET", "/ui")]
    assert _owners(e).get(("GET", "/ui")) == {"web_ui"}


def test_own_spec_scope_prose_still_owns(tmp_path):
    # the collapsed core's OWN scope list (not an inherited quote) keeps
    # registering ownership — the line filter must not blind the fallback
    e = _engine(tmp_path)
    core = {"id": "core", "title": "product core",
            "requirement": "one module owns the whole base product",
            "spec_markdown": ("Declared routes (all owned by `src/core.py`):\n"
                              "- POST /notes\n- GET /notes\n- GET /health\n")}
    owned = set(e._leaf_owned_routes(core))
    assert {("POST", "/notes"), ("GET", "/notes"),
            ("GET", "/health")} <= owned, owned


def test_amend_node_still_owns_nothing(tmp_path):
    e = _engine(tmp_path)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(core)
    amend = {"id": "beautify", "title": "make notes pretty",
             "code_target": "src/core.py",
             "exposes": ["post_notes(payload, query)"],
             "requirement": "polish the POST /notes output"}
    assert e._leaf_owned_routes(amend) == [], (
        "an amend node (code_target) owns nothing — exposes or prose")
