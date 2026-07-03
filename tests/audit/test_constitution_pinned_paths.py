"""S10.22 — constitution-pinned module paths are IMMOVABLE engine data.

v155 (p6-micro-notes): the case constitution pins a module path — "storage
through sqlite3 in src/db.py" (HUMAN non-negotiable data). The small-product
collapse swallowed storage into src/core.py and the S10.20 durable purge then
retargeted EVERY db.py reference at core.py — so the written spec openly
contradicted the constitution and the LLM spec reviewer rightly REJECTED it
forever (trace events 14-24: reject x3, doctor oscillated reconcile_check →
goal_coverage → redecompose_parent, rework budget exhausted, root RED with
open cause L0:task_check_mismatch). Earlier READY runs (v152) silently
violated the SAME rule — no src/db.py was ever built; the reviewer just could
not see it because the specs still SAID db.py.

The fix is in the ENGINE, never in the reviewer or the case: a module path a
constitution rule names literally is pinned DATA every engine transformation
must respect —
  1. a deterministic extractor collects the pinned paths (entry excluded —
     entry synthesis already owns it);
  2. the small-product collapse keeps a pinned non-entry module as its OWN
     child leaf instead of swallowing it;
  3. the purge / durable spec-write sanitizer never retargets a pinned stem;
  4. the plan-ownership gate reds when a pinned path has NO owner node in the
     realized plan (this is the gate that would have redded v152's silent
     violation).
"""
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402

_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False, "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True, "testable_criteria": True}
_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}

# the REAL p6 constitution rule (verbatim class): entry src/app.py named first,
# storage pinned at src/db.py
PINNED_RULE = ("Standard library ONLY: HTTP through a WSGI app (src/app.py "
               "exposes wsgi_app), storage through sqlite3 in src/db.py. "
               "No third-party packages.")

PINNED_PROJECT = {
    "name": "micro-notes-pinned",
    "goal": ("A tiny notes service over WSGI: POST /notes stores {text} and"
             " returns {id}; GET /notes returns the items. src/app.py exposes"
             " wsgi_app."),
    "target": "POST then GET round-trips a note; sqlite3 stdlib only",
    "constitution": [PINNED_RULE],
    "acceptance": {"smoke": ["the build succeeds"]},
    "policy": dict(_POLICY),
}


def _bare_engine(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._constitution = [PINNED_RULE]
    e._product_contract = lambda: {"entry": "src/app.py", "boot": {},
                                   "routes": []}
    return e


# ── extractor: literal src/<name>.py in constitution rules, entry excluded ───

def test_extractor_collects_pinned_paths_excluding_entry(tmp_path):
    e = _bare_engine(tmp_path)
    pins = e._constitution_pinned_paths()
    assert [p for p, _ in pins] == ["src/db.py"], (
        "the rule pins src/db.py; src/app.py is the declared entry and is"
        " handled by entry synthesis — it must be excluded")
    assert pins[0][1] == PINNED_RULE, (
        "the finding must carry the exact constitution rule that pins the"
        " path (attributability)")
    e._constitution = ["No third-party packages."]
    assert e._constitution_pinned_paths() == [], (
        "a rule naming no src path pins nothing")


# ── collapse honors the pin: the pinned module keeps its OWN leaf ─────────────

def _rejected_plan_decomposer(ctx):
    # same shape as the S10.7/S10.14 collapse fixtures: a 2-child plan the
    # small-product floor rejects, whose prose plans src/db.py + src/app.py
    if ctx["depth"] == 0:
        return {"atomic": False, "metrics": dict(_BIG),
                "spec_markdown": ("## Scope\nIn:\n"
                                  "- src/db.py: connect/add_note/list_notes"
                                  " (sqlite storage)\n"
                                  "- src/app.py: wsgi_app dispatching the"
                                  " routes\n"),
                "children": [{"id": "db_persistence",
                              "title": "sqlite storage layer"},
                             {"id": "wsgi_handler",
                              "title": "WSGI route handlers"}]}
    return {"metrics": dict(_SMALL),
            "acceptance": ["Given a note, When POSTed to /notes, Then GET"
                           " /notes returns it"]}


def test_collapse_keeps_pinned_module_own_leaf(plugin, tmp_path):
    from harness import auto_implementer
    res = eng.run_project(dict(PINNED_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _rejected_plan_decomposer,
                                  "implementer": auto_implementer.implement})
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "small product" in dump, "the floor must collapse this run"
    # 1. the pinned module has its OWN owner node in the realized plan —
    # its spec was written like any other visited leaf
    db_spec = tmp_path / "wk" / "specs" / "db.md"
    assert db_spec.is_file(), (
        "the collapse must keep constitution-pinned src/db.py as its OWN"
        " child leaf (v155: it was swallowed into src/core.py and the"
        " reviewer rejected the spec forever)")
    # 2. the purge never rewrote the pinned references: db.py survives in
    # the written specs instead of being retargeted at core.py
    specs = {p.name: p.read_text(encoding="utf-8")
             for p in sorted((tmp_path / "wk" / "specs").glob("*.md"))}
    assert any("src/db.py" in t for t in specs.values()), (
        "every src/db.py reference was retargeted — the purge treated a"
        " constitution-pinned path as a droppable module")
    # 3. no spec contradicts the constitution: the collapsed core must not
    # claim to BE the storage layer while db.py is pinned — storage goes
    # THROUGH the pinned module
    core = specs.get("core.md", "")
    assert core, "the collapsed core leaf must still write its spec"
    assert "AND the storage" not in core, (
        "core.md still orders storage INTO src/core.py — the exact"
        " constitution contradiction the v155 reviewer rejected")
    assert "src/db.py" in core, (
        "core's spec must direct storage access THROUGH the pinned module")
    # 4. the ownership card gate stays clean (the pin is a KNOWN owner)
    assert "NO node owns" not in dump, (
        "a pin-honoring collapse must produce ZERO ownership findings")
    # 5. and the plan gate has nothing to red about
    assert "constitution-pinned module src/db.py has NO owner" not in dump


# ── plan gate: an ownerless pinned path is an explicit red finding ────────────

def test_plan_gate_reds_ownerless_pinned_path(tmp_path):
    e = _bare_engine(tmp_path)
    e._node_registry = {"L0": "root", "core": "product core"}
    pin = [f for f in e._plan_ownership_report()
           if "constitution-pinned" in f]
    assert pin, (
        "a realized plan with NO owner node for constitution-pinned"
        " src/db.py must yield an explicit plan finding (the gate that"
        " would have redded v152's silent violation)")
    assert "src/db.py" in pin[0] and "storage through sqlite3" in pin[0], (
        "the finding must name the pinned path AND the constitution rule")
    # green: an owner node exists → no pinned finding
    e._node_registry["db"] = "sqlite storage (pinned)"
    assert not [f for f in e._plan_ownership_report()
                if "constitution-pinned" in f]
    # green: a constitution pinning nothing reports exactly as before
    e._constitution = ["No third-party packages."]
    assert e._plan_ownership_report() == []


# ── purge guard: a pinned stem is never retargeted (unit red/green) ───────────

def test_purge_never_retargets_pinned_stem(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    e._node_registry = {"L0": "root"}
    root = {"id": "L0", "title": "root",
            "spec_markdown": ("In:\n- src/db.py: storage (constitution-"
                              "pinned)\n- src/junk.py: leftover of the"
                              " rejected plan\n- src/app.py: entry")}
    e._purge_dropped_module_refs(root, "core")
    assert "src/db.py" in root["spec_markdown"], (
        "the purge retargeted a constitution-pinned path — pinned stems are"
        " IMMOVABLE data")
    assert "src/junk.py" not in root["spec_markdown"], (
        "a genuinely foreign module must still be retargeted (the guard must"
        " not weaken the purge)")
    # the durable spec-write door sanitizer honors the pin too
    e.workspace.spec("late_node", "late", 1, "leaf", "", "L0", [],
                     node={"id": "late_node",
                           "spec_markdown": ("reuse src/db.py storage; also"
                                             " src/junk.py helpers")})
    text = (tmp_path / "wk" / "specs" / "late_node.md").read_text(
        encoding="utf-8")
    assert "src/db.py" in text, (
        "a spec written AFTER the purge lost its pinned reference — the"
        " durable sanitizer must never retarget a pinned stem")
    assert "src/junk.py" not in text, (
        "the dropped stem must still be sanitized at the write door")
