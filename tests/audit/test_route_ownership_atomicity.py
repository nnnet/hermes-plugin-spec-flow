"""Audit STAGE 10 — route-ownership atomicity (the v152 postmortem, pinned).

v152 (p6-micro-notes, ticks 111-115) shipped READY over a DETECTED atomicity
violation: '_plan_ownership_report' logged "route POST /notes: 3 owner leaves
(L0, core, web_ui) — duplicate" with a NEUTRAL verdict at root integrate
(ticks 111-113), the product went READY at tick 114 and the doctor closed
with "treatments": 0 at tick 115. Investigator findings #1-#3:

- S10.16 (finding #1): the ownership DATUM was imprecise — the ROOT branch L0
  entered `_route_owners` as an owner of every route (its text names the whole
  product by construction). Ownership means "this LEAF BUILDS the route":
  only childless leaves may enter the datum.
- S10.17 (finding #3): the duplicate finding was informational-only — zero
  treatments, READY passed. RULE_ROUTE_OWNERSHIP (exactly ONE owner leaf) is
  the engine's own rule; detecting its violation and suppressing it from the
  verdict is a dishonest terminal. A duplicate must FAIL root integrate, feed
  the doctor, and an UNRESOLVED duplicate must end NOT READY. Orphan findings
  (0 owners) stay informational (serving is Phase 7's boot/suite authority).
- S10.18 (finding #2): the web_ui spec was authored by copy-pasting the frozen
  JSON API routes (POST /notes, GET /notes) with contradictory HTML semantics;
  core.py and web_ui.py both defined post_notes/get_notes, assembly adopted
  core's and web_ui's became dead rival code. The card gate must red EARLY:
  a leaf spec claiming a route ANOTHER node already owns in `_route_owners`
  gets a finding naming the owner and demanding removal of the foreign route
  text or explicit re-ownership through decomposition. Amend nodes
  (code_target) stay exempt — they QUOTE the owner's source (S10.10/v151).
"""
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import auto_implementer, run_engine as eng  # noqa: E402

_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}


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


# ── S10.16 the ownership datum admits ONLY childless leaves ──────────────────

def test_branch_node_never_enters_ownership_datum(tmp_path):
    # v152 finding #1: the ROOT's text names every route by construction —
    # recording it as an owner turned every route into a phantom duplicate
    e = _engine(tmp_path)
    branch = {"id": "l0", "title": "notes product",
              "requirement": "serve POST /notes, GET /notes and GET /health",
              "children": [{"id": "core", "title": "core"}]}
    assert e._leaf_owned_routes(branch) == [], (
        "a node WITH children does not build routes — ownership belongs to"
        " its leaves")
    assert not any("l0" in o for o in _owners(e).values()), (
        "a branch/root node must never be recorded in _route_owners (v152:"
        " L0 counted as an owner of POST /notes, GET /notes, GET /health)")


def test_childless_leaf_still_enters_datum(tmp_path):
    e = _engine(tmp_path)
    leaf = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(leaf), "a childless leaf owns what it names"
    assert any("core" in o for o in _owners(e).values())


def test_rederivation_drops_stale_ownership(tmp_path):
    # precision over time: a reworked spec that DROPS a route claim must fall
    # out of the datum — sets that only grow would keep a resolved duplicate
    # red forever at root integrate
    e = _engine(tmp_path)
    leaf = {"id": "web_ui", "title": "ui",
            "requirement": "GET /health page; also POST /notes"}
    e._leaf_owned_routes(leaf)
    assert "web_ui" in _owners(e).get(("POST", "/notes"), set())
    leaf["requirement"] = "GET /health page only"
    e._leaf_owned_routes(leaf)
    assert "web_ui" not in _owners(e).get(("POST", "/notes"), set()), (
        "re-derivation must drop the stale POST /notes row for web_ui")
    assert "web_ui" in _owners(e).get(("GET", "/health"), set())


# ── S10.17 an UNRESOLVED duplicate owner is an honest NOT READY ──────────────
# Dynamic, offline fake agents. > 5 declared routes so the small-product floor
# does not collapse the rival leaves away. The gate reads ONLY the
# _route_owners datum — no per-scenario special case.

WEB_PROJECT = {
    "name": "micro-notes",
    "goal": ("A notes service over WSGI: POST /notes stores {text}; GET /notes"
             " lists items; GET /health answers 200; GET /stats counts notes;"
             " DELETE /notes clears them; GET /ui renders an HTML list;"
             " GET /export returns the notes as CSV. src/app.py exposes"
             " wsgi_app."),
    "target": "POST then GET round-trips a note; sqlite3 stdlib only",
    "constitution": ["Standard library only."],
    "acceptance": {"smoke": ["the build succeeds"]},
    "policy": dict(_POLICY),
}

_ACC = ["Given a note, When POSTed to /notes, Then GET /notes returns it"]


def _dup_owner_decomposer(ctx):
    # the v152 shape: web_ui's spec copy-pastes the frozen JSON API routes the
    # core leaf already owns — TWO childless leaves claim the same route
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [
                    {"id": "core",
                     "title": "store and list notes via POST /notes and"
                              " GET /notes"},
                    {"id": "web_ui",
                     "title": "HTML list at GET /ui; also serves POST /notes"
                              " and GET /notes"}]}
    return {"metrics": dict(_SMALL), "acceptance": list(_ACC)}


def _single_owner_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [
                    {"id": "core",
                     "title": "store and list notes via POST /notes and"
                              " GET /notes"},
                    {"id": "web_ui", "title": "HTML list at GET /ui"}]}
    return {"metrics": dict(_SMALL), "acceptance": list(_ACC)}


def _run(tmp_path, plugin, decomposer):
    return eng.run_project(dict(WEB_PROJECT),
                           workspace=str(tmp_path / "wk"), depth="product",
                           tools=plugin.tools,
                           contracts_dir=str(eng.CONTRACTS),
                           agents={"decomposer": decomposer,
                                   "implementer": auto_implementer.implement})


def test_duplicate_route_ownership_is_honest_not_ready(plugin, tmp_path):
    res = _run(tmp_path, plugin, _dup_owner_decomposer)
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "duplicate route ownership" in dump, (
        "a duplicate owner detected in _route_owners must surface as an"
        " explicit root-integrate FAIL milestone (v152 ticks 111-113: the"
        " finding was logged with a neutral verdict and READY sailed through)")
    assert res.product_status == "NOT READY", (
        "an UNRESOLVED duplicate route owner violates RULE_ROUTE_OWNERSHIP —"
        " the terminal verdict must be an honest NOT READY (v152: READY at"
        " tick 114, doctor treatments: 0 at tick 115)")


# ── S10.18 the card gate reds EARLY on a foreign-route spec claim ────────────
# v152 finding #2: web_ui's spec copy-pasted the frozen JSON API routes with
# contradictory HTML semantics — core.py and web_ui.py both defined
# post_notes/get_notes; assembly adopted core's, web_ui's became dead rival
# code. With S10.16+S10.17 that reds at ROOT integrate — but that is LATE.
# The card gate must red at the LEAF, naming the owner.

def test_card_gate_reds_on_foreign_route_claim(tmp_path):
    e = _engine(tmp_path)
    owner = {"id": "core", "title": "core notes",
             "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(owner)
    rival = {"id": "web_ui", "title": "web ui",
             "requirement": "render an HTML list; also serve POST /notes",
             "acceptance": ["Given a note, When POSTed, Then HTML shows it"]}
    finds = e._card_completeness_findings(rival)
    assert any("core" in f and "/notes" in f for f in finds), (
        "a leaf spec claiming a route ANOTHER node already owns must red at"
        " the card gate NAMING the owner (v152: web_ui copy-pasted core's"
        f" routes and shipped dead rival handlers) — got: {finds!r}")


def test_card_gate_green_on_own_new_route(tmp_path):
    e = _engine(tmp_path)
    owner = {"id": "core", "title": "core notes",
             "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(owner)
    mine = {"id": "web_ui", "title": "web ui",
            "requirement": "render the HTML list at GET /ui",
            "acceptance": ["Given notes, When GET /ui, Then HTML lists them"]}
    assert e._card_completeness_findings(mine) == [], (
        "a leaf claiming ONLY its own (sole-owner) route is legitimate — the"
        " gate must stay silent")


def test_card_gate_amend_node_stays_exempt(tmp_path):
    # the S10.10/v151 class: an amend node QUOTES the owner's source as edit
    # context — it owns nothing and needs no card; never regress this
    e = _engine(tmp_path)
    owner = {"id": "core", "title": "core notes",
             "requirement": "serve POST /notes and GET /notes"}
    assert e._leaf_owned_routes(owner)
    amend = {"id": "beautify", "title": "make notes pretty",
             "code_target": "src/core.py",
             "requirement": "polish the POST /notes and GET /notes output"}
    assert e._card_completeness_findings(amend) == [], (
        "an amend node (code_target) quoting the owner's routes is exempt"
        " from the foreign-surface card gate")


def test_single_owner_plan_has_no_duplicate_event(plugin, tmp_path):
    res = _run(tmp_path, plugin, _single_owner_decomposer)
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "duplicate route ownership" not in dump, (
        "a clean single-owner plan must produce ZERO duplicate-ownership"
        " events — the gate must never red on a legitimate plan")
    assert not any("duplicate route ownership" in str(lp.get("detail", ""))
                   for lp in res.loops), (
        "no integrate-fail loop entry may be recorded for a clean plan")
