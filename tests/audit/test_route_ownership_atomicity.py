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
