"""Audit STAGE 10 — assembly-seam honesty (the v149 postmortem, pinned).

v149 reached an honest NOT READY, but BOTH root causes were DESIGN holes the
audit should have redded before the run:

1. The card contracted no SUCCESS STATUS for a route — the coder returned 201
   while the tester asserted 200. Two independent guesses that could only
   collide at assembly, where the doctor cannot fix a disagreement that lives
   in the CARD. ("assemble deterministically WITHOUT guessing" violated.)
2. The verification suite was not HERMETIC — a tester-invented phantom module
   (`from db import ...`) was satisfied by an UNRELATED repo through a host
   editable-install .pth, turning an honest ModuleNotFoundError into a
   misleading cross-project ImportError. And the deterministic import repair
   never covered tests/ (and was dormant behind PRE_GATE in real runs).

Stage-wide principle: every value TWO independent artifacts must agree on
(status codes, symbol names, module owners) must exist as ONE engine-declared
datum both sides read — and the suite that judges the product must see ONLY
the product.
"""
import pathlib
import subprocess
import sys
import textwrap

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

WEB_PROJECT = {
    "name": "micro-notes",
    "goal": ("A tiny notes service over WSGI: POST /notes stores {text} and"
             " returns {id}; GET /notes returns the items. src/app.py exposes"
             " wsgi_app."),
    "target": "POST then GET round-trips a note; sqlite3 stdlib only",
    "constitution": ["Standard library only."],
    "acceptance": {"smoke": ["the build succeeds"]},
    "policy": dict(_POLICY),
}


# ── S10.1 success status is ONE engine-declared datum ────────────────────────

def test_success_status_single_source():
    assert eng._route_success_status("POST") == 201
    assert eng._route_success_status("post") == 201
    for m in ("GET", "PUT", "PATCH", "DELETE", None, ""):
        assert eng._route_success_status(m) == 200, m


# ── S10.2 assembly import repair covers tests/ and stays honest ──────────────

def _mini_ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "core.py").write_text(textwrap.dedent("""\
        def connect(path=None):
            return object()

        def store_note(conn, text):
            return 1

        def list_notes(conn=None):
            return []
    """), encoding="utf-8")
    return ws


def test_assembly_repair_repoints_phantom_test_import(tmp_path):
    ws = _mini_ws(tmp_path)
    t = ws / "tests" / "test_core.py"
    t.write_text("from db import connect, list_notes, store_note\n",
                 encoding="utf-8")
    notes = eng._repair_workspace_imports(str(ws))
    assert notes, "the unique-owner repair must fire on a phantom test import"
    fixed = t.read_text(encoding="utf-8")
    assert "from core import" in fixed and "from db import" not in fixed, (
        "tests/ imports must be re-pointed to the REAL owner exactly like"
        f" src/ imports (v149) — got: {fixed!r}")


def test_assembly_repair_leaves_unowned_symbols_honest(tmp_path):
    ws = _mini_ws(tmp_path)
    t = ws / "tests" / "test_x.py"
    t.write_text("from db import no_such_symbol\n", encoding="utf-8")
    eng._repair_workspace_imports(str(ws))
    assert "from db import no_such_symbol" in t.read_text(encoding="utf-8"), (
        "a symbol NO src module owns must stay untouched — rewriting it would"
        " be guessing; the honest outcome is a red import")


# ── S10.3 the oracle is hermetic: host pollution cannot satisfy an import ────

def test_verify_suite_is_hermetic_against_host_pth(plugin, tmp_path):
    from harness import auto_implementer

    def _decomposer(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BIG),
                    "children": [{"id": "notes_api",
                                  "title": "store and list notes"}]}
        return {"metrics": dict(_SMALL)}

    res = eng.run_project(dict(WEB_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _decomposer,
                                  "implementer": auto_implementer.implement})
    assert res is not None
    ws = tmp_path / "wk"
    conftest = ws / "conftest.py"
    assert conftest.exists() and "oracle isolation" in conftest.read_text(
        encoding="utf-8"), (
        "the engine must plant its oracle-isolation conftest before judging"
        " the product (v149: a host .pth satisfied a phantom import with"
        " foreign code)")
    # live proof: a decoy package visible through PYTHONPATH (the .pth shape)
    # must be INVISIBLE to the suite under the engine's conftest
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "phantom_dep.py").write_text("VALUE = 42\n", encoding="utf-8")
    probe = ws / "tests" / "test_hermetic_probe.py"
    probe.write_text(textwrap.dedent("""\
        import pytest

        def test_phantom_is_invisible():
            with pytest.raises(ModuleNotFoundError):
                import phantom_dep  # noqa: F401
    """), encoding="utf-8")
    try:
        p = subprocess.run(
            ["python3", "-m", "pytest", "-q", "--no-header",
             f"--confcutdir={ws}", "-p", "no:cacheprovider",
             "--import-mode=importlib", "tests/test_hermetic_probe.py"],
            capture_output=True, text=True, timeout=120, cwd=str(ws),
            env={**__import__("os").environ, "PYTHONPATH": str(decoy)})
    finally:
        probe.unlink(missing_ok=True)
    assert p.returncode == 0, (
        "with the engine conftest in place, a PYTHONPATH decoy must stay"
        f" invisible to the suite — output:\n{p.stdout}\n{p.stderr}")


# ── S10.5 a spec planning OWNERLESS src files is red at the card gate ────────
# v150 core.md: the LLM spec planned `src/db.py` + `src/app.py` inside ONE
# atomic leaf (own metrics: modules=1) — files with no node, no card, no gate;
# the coder/tester then chased them (the v149 phantom `from db import ...` was
# ORDERED by that prose). The card gate must red this and send the spec back.

def _multifile_spec_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(_BIG),
                "children": [{"id": "notes_core",
                              "title": "store and list notes via POST /notes"
                                       " and GET /notes"}]}
    if ctx.get("rework"):
        # the honest rework: same concern, retargeted at the leaf's own module
        return {"metrics": dict(_SMALL),
                "acceptance": ["Given a note, When POSTed to /notes, Then GET"
                               " /notes returns it"],
                "spec_markdown": ("## Scope\nIn:\n- src/notes_core.py: sqlite"
                                  " storage + route handlers\n")}
    return {"metrics": dict(_SMALL),
            "acceptance": ["Given a note, When POSTed, Then it is stored"],
            "spec_markdown": ("## Scope\nIn:\n- src/db.py: sqlite storage\n"
                              "- src/logic.py: note handling\n")}


def test_ownerless_file_plan_is_red_at_the_card(plugin, tmp_path):
    from harness import auto_implementer
    # 6 declared routes > the small-product floor (5), so the base is NOT
    # collapsed into an engine-made core leaf and the decomposer's own
    # (poisoned) spec_markdown reaches the card gate
    project = dict(WEB_PROJECT)
    project["goal"] = (
        "A notes service over WSGI: POST /notes stores {text}; GET /notes"
        " lists items; GET /health answers 200; GET /stats counts notes;"
        " DELETE /notes clears them; GET /ui renders an HTML list;"
        " GET /export returns the notes as CSV. src/app.py exposes wsgi_app.")
    res = eng.run_project(project,
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _multifile_spec_decomposer,
                                  "implementer": auto_implementer.implement})
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "NO node owns" in dump, (
        "a leaf spec planning src files that no node owns must red at the"
        " card gate (v150 core.md smuggled a 2-file mini-architecture past"
        " the graph; the v149 phantom import was ordered by that prose)")


# ── S10.4 a test contradicting the contracted status is red AT THE LEAF ──────

def _status_lying_implementer(ctx):
    ws = ctx["workspace"]
    node = ctx.get("node", "x")
    fn = eng._snake(node)
    ws._write(f"src/{fn}.py", textwrap.dedent("""\
        _DB = []


        def post_notes(payload, query):
            _DB.append(dict(payload or {}))
            return 201, {"id": len(_DB)}


        def get_notes(payload, query):
            return 200, {"items": list(_DB)}
    """), "status-lying leaf: correct code")
    ws._write(f"tests/test_{fn}.py", textwrap.dedent("""\
        def _call(method, path):
            return "200 OK"


        def test_roundtrip():
            status = _call("POST", "/notes")
            assert status == "200 OK"
    """), "status-lying leaf: test asserts the WRONG success status")


def test_status_lying_test_is_red_at_the_leaf(plugin, tmp_path):
    def _decomposer(ctx):
        if ctx["depth"] == 0:
            return {"metrics": dict(_BIG),
                    "children": [{"id": "notes_api",
                                  "title": "own POST /notes and GET /notes"}]}
        return {"metrics": dict(_SMALL)}

    res = eng.run_project(dict(WEB_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _decomposer,
                                  "implementer": _status_lying_implementer})
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "test-status gate" in dump, (
        "a leaf test asserting 200 for a route the card contracts at 201 must"
        " red AT THE LEAF with the exact expected value (v149: the collision"
        " surfaced only at assembly, where the doctor cannot fix the card)")


# ── S10.7 the small-product collapse REWRITES the inherited spec ─────────────
# v150 root cause: the decomposer proposed db_persistence + wsgi_handler, the
# floor collapsed them into ONE core leaf — but the core INHERITED the
# spec_markdown authored for the REJECTED plan, so `src/db.py` stayed in the
# prose and steered the coder/tester at the phantom module. The collapse must
# rewrite the spec deterministically for one module and come out clean through
# the same "NO node owns" ownership gate.

def _rejected_plan_decomposer(ctx):
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
    # the card-fill rework round for the engine-made core leaf
    return {"metrics": dict(_SMALL),
            "acceptance": ["Given a note, When POSTed to /notes, Then GET"
                           " /notes returns it"]}


def test_collapse_rewrites_spec_and_is_gate_clean(plugin, tmp_path):
    from harness import auto_implementer
    # WEB_PROJECT declares <= 5 routes -> the floor collapses the rejected
    # 2-child plan into one engine-made core leaf
    res = eng.run_project(dict(WEB_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _rejected_plan_decomposer,
                                  "implementer": auto_implementer.implement})
    assert res is not None
    dump = "\n".join(repr(ev) for ev in res.events)
    assert "small product" in dump, "the floor must collapse this run"
    spec = tmp_path / "wk" / "specs" / "core.md"
    assert spec.is_file(), "the collapsed core leaf must write its spec"
    text = spec.read_text(encoding="utf-8")
    assert "src/db.py" not in text, (
        "the collapse must NOT inherit the spec authored for the rejected"
        " plan (v150: src/db.py survived in core.md and the coder/tester"
        " chased a module no node owns)")
    assert "NO node owns" not in dump, (
        "a clean collapse rewrite must produce ZERO ownership findings —"
        " a finding means the engine-rewritten spec still plans foreign"
        " src files")


# ── S10.8 engine rules are ONE source: ctx -> decomposer prompt ──────────────
# Everything the code later checks/applies to the plan (small-product floor,
# leaf thresholds, fan-out cap, route/module ownership, success statuses) is
# handed to the decomposer UP FRONT via ctx["engine_rules"], and the harness
# prompt renders those exact values — no number is re-hardcoded in prompt text
# or in this test (every expected value comes from the engine constants).

def test_engine_rules_reach_ctx_and_prompt(plugin, tmp_path, monkeypatch,
                                           fake_openai):
    import json
    from harness import auto_implementer
    from harness import llm_backend as lb
    from harness import llm_decomposer as dc
    from harness_fakeapi import ok

    seen = {}

    def _capturing_decomposer(ctx):
        seen.setdefault("ctx", ctx)
        return {"atomic": True, "metrics": dict(_SMALL),
                "acceptance": ["Given a note, When POSTed to /notes, Then GET"
                               " /notes returns it"]}

    res = eng.run_project(dict(WEB_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools, contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _capturing_decomposer,
                                  "implementer": auto_implementer.implement})
    assert res is not None
    ctx = seen["ctx"]
    er = ctx["engine_rules"]
    assert er["small_product_routes_le"] == eng.SMALL_PRODUCT_ROUTES_DEFAULT
    assert er["leaf_max_modules"] == eng._gates.MAX_MODULES
    assert er["leaf_max_tasks"] == eng._gates.MAX_TASKS
    assert er["leaf_max_interfaces"] == eng._gates.MAX_INTERFACES
    assert er["leaf_max_loc"] == eng._gates.MAX_LOC
    assert er["route_ownership"] == eng.RULE_ROUTE_OWNERSHIP
    assert er["atomic_leaf_module"] == eng.RULE_ATOMIC_LEAF_MODULE
    assert er["route_success_status"] == {
        "POST": eng._route_success_status("POST"),
        "other": eng._route_success_status("GET")}

    # the harness renders the SAME ctx values into the prompt it actually
    # sends (captured through a real local server, no mocks on the LLM path)
    monkeypatch.delenv("SPEC_FLOW_DECOMPOSER_TEAM", raising=False)
    monkeypatch.setattr(dc, "MODEL", "openrouter/x:free")
    lb.configure_workers(None)
    reply = json.dumps({"atomic": True, "metrics": dict(_SMALL)})
    srv = fake_openai([(200, ok(reply))])
    dc.decompose(ctx)
    sent = " ".join(m.get("content", "")
                    for m in srv.requests[0]["messages"])
    assert ("<= %s route(s) is collapsed"
            % er["small_product_routes_le"]) in sent
    assert ("at most %s children" % er["max_children"]) in sent
    assert ("modules <= %s" % er["leaf_max_modules"]) in sent
    assert ("estimated_loc <= %s" % er["leaf_max_loc"]) in sent
    assert ("POST -> %s" % er["route_success_status"]["POST"]) in sent
    assert er["route_ownership"] in sent


# ── S10.9 a tiny-but-real leaf module is NOT "lost" ──────────────────────────
# v151: src/ping_text.py delivered `def get_ping(payload, query): return 200,
# "pong"` — present on the final tree, imported, serving its route — yet the
# single-authority check flagged it "absent/empty" because it counts CODE
# LINES (<3 == lost). A magic size threshold conflates "small by design" with
# "orphaned in git"; that one false FAIL opened a doctor cause and
# single-handedly flipped the run NOT READY. Lost-ness must be judged by WHAT
# THE FILE DEFINES, never by its size.

def test_tiny_real_leaf_module_is_not_lost():
    lost = eng._leaf_code_lost
    tiny = ('# code: src/ping_text.py\n# test: tests/test_ping_text.py\n'
            'def get_ping(payload, query):\n    return 200, "pong"\n')
    assert not lost(tiny), (
        "a one-function module is REAL delivery — flagging it lost is the"
        " v151 false red (size thresholds are not evidence of orphaning)")
    assert lost(""), "an empty file IS lost"
    assert lost("# just a comment\n"), "a comment-only stub IS lost"
    assert lost("pass\n"), "a pass-only stub IS lost"
    assert not lost("ROUTES = {('GET', '/x'): 'h'}\n"), (
        "a module whose only payload is a dispatch table still defines data")
    assert not lost("def broken(:\n"), (
        "a syntactically broken file EXISTS — 'delivered but broken' is the"
        " assembled suite's verdict to give, not the orphan check's")


# ── S10.10 route ownership is DATA, not prose grep ───────────────────────────
# v151: every amend spec EMBEDS the owner module's source as context
# ("### Current `src/core.py` ..."), so the plan report's spec-text grep saw
# POST /notes in SEVEN specs and reported "7 owner leaves — duplicate" for
# every declared route. Ownership already exists as an engine datum
# (_leaf_owned_routes: amend nodes own nothing); the report must READ that
# datum, never re-derive it from prose (Charter P5: prose is not data).

def test_ownership_report_reads_data_not_prose(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    ws = e.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    specs = pathlib.Path(ws.root) / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    # the REAL owner names its routes in its own requirement …
    core = {"id": "core", "title": "notes service",
            "requirement": "serve POST /notes, GET /notes and GET /health"}
    assert e._leaf_owned_routes(core), "sanity: core must own the routes"
    # … while an amend leaf merely QUOTES the owner's source as edit context
    (specs / "core.md").write_text(
        "serves POST /notes, GET /notes, GET /health", encoding="utf-8")
    (specs / "beautify.md").write_text(
        "EDIT src/core.py in place.\n### Current `src/core.py`\n"
        "```python\nROUTES = {('POST', '/notes'): 'create',"
        " ('GET', '/notes'): 'list', ('GET', '/health'): 'health'}\n```\n",
        encoding="utf-8")
    rep = e._plan_ownership_report()
    assert not any("duplicate" in f for f in rep), (
        "quoting another module's source in an amend spec must NOT create"
        f" route ownership (v151 phantom '7 owner leaves') — got: {rep}")


# ── S10.11 node ids are engine-normalized ASCII snake_case ───────────────────
# v151: a human mid-run note was adopted as node «красивый_вид» — a non-ASCII
# id the engine accepted silently into FILE NAMES (specs/<id>.md and, for a
# module leaf, src/<id>.py). Ids are engine-owned data that become paths; the
# engine must normalize them deterministically (journaling the rename), never
# trust the raw string from a decomposer or an injection.

def test_node_ids_normalized_to_ascii():
    norm = eng._ascii_node_id
    assert norm("красивый_вид", "l0_part5", set()) == "l0_part5", (
        "a foreign-script id must fall back to the deterministic ASCII name")
    assert norm("Get-Notes", "x", set()) == "get_notes"
    assert norm("core", "x", set()) == "core"
    assert norm("красивый_вид", "l0_part5", {"l0_part5"}) == "l0_part5_2"


def test_requirement_nodes_carry_ascii_ids(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._standing_requirements = lambda: [
        ("красивый_вид", "make the notes nice to read")]
    nodes = e._requirement_nodes(scope=None)
    assert nodes, "sanity: the standing requirement must materialize"
    nid = nodes[0]["id"]
    assert nid.isascii() and nid == eng._snake(nid), (
        f"a standing-requirement node id must be ASCII snake_case, got {nid!r}"
        " (v151: «красивый_вид» flowed into workspace file names)")
    # determinism: the SAME raw name maps to the SAME id on every poll —
    # otherwise coverage bookkeeping breaks and the requirement re-attaches
    assert e._requirement_nodes(scope=None)[0]["id"] == nid


# ── S10.12 ONE write door: delivered code is English/ASCII ───────────────────
# v151 follow-up: hygiene rules (English identifiers/comments, no foreign
# scripts, no absolute host paths) must live at the SINGLE write seam every
# writer passes through — not as N per-writer checks. The rule text is ONE
# engine constant (RULE_CODE_STYLE) rendered into worker prompts and enforced
# by the same lint the door runs; prompt and gate can never drift apart.
# Human PROSE (specs, notes) is data and may be any language — only code
# files (src/*.py, tests/*.py) are linted.

def test_delivery_lint_red_and_green():
    lint = eng._delivery_lint
    assert lint("src/получить.py", "X = 1\n"), "non-ascii file path must red"
    assert lint("src/m.py", "def получить():\n    return 1\n"), (
        "a non-ascii identifier becomes the product's API — must red")
    assert lint("src/m.py", "# комментарий\nX = 1\n"), (
        "cyrillic in a code file (comments included) must red")
    assert lint("src/m.py", "P = '/home/user/x'\n"), (
        "an absolute host path in delivered code must red (v149 boundary)")
    # green edges: honest code passes, human prose is NOT linted
    assert lint("src/m.py", "def ok():\n    return 'fine'\n") == []
    assert lint("specs/plan.md", "русская проза от человека") == [], (
        "prose artifacts are data — any language is legitimate")


def test_write_door_refuses_dirty_code(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    ws = e.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    ws._write("src/m.py", "def получить():\n    return 1\n", "code")
    assert not (pathlib.Path(ws.root) / "src" / "m.py").exists(), (
        "dirty code must never LAND — the door refuses, not just warns")
    assert any(str(a.get("type", "")).startswith("refused")
               for a in ws.artifacts), "the refusal must leave a record"
    ws._write("src/m.py", "def ok():\n    return 1\n", "code")
    assert (pathlib.Path(ws.root) / "src" / "m.py").exists(), (
        "clean code passes the same door untouched")


def test_code_style_rule_reaches_worker_prompts():
    import importlib
    rw = importlib.import_module("harness.role_worker")
    blk = rw._code_style_block()
    assert eng.RULE_CODE_STYLE in blk, (
        "workers must be TOLD the rule the door enforces (same constant)")
    src = pathlib.Path(rw.__file__).read_text(encoding="utf-8")
    assert src.count("_code_style_block()") >= 2, (
        "both the implementer and the reviewer systems must carry the rule")
