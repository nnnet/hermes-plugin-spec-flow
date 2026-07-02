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
