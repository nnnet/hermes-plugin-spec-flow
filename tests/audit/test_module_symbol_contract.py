"""S11 — inter-module SYMBOL contracts (v149/v150/v156/v157 meta-class).

The class: a value TWO leaves must agree on — the symbol surface between an
exporting module and its importer — never existed as ONE engine-declared
datum before both sides were coded. Each LLM guessed, the collision surfaced
only at assembly, and the doctor's rework re-guessed:

  * v157 (p6-micro-notes): src/core.py line 3 ``from db import init_db,
    store_note, list_notes`` while src/db.py exported DIFFERENT names —
    ImportError at boot, the whole suite + smoke red, doctor churned;
  * v156: the integrate rework introduced ``db.list_notes()`` while src/db.py
    defined get_notes (S10.25 caught the attr form; ``from X import Y``
    stayed exempt and order-dependent);
  * v149/v150: the same meta-class on status codes and response bodies —
    solved for HTTP routes by contracts/interface.json (single source, both
    sides read it, gates enforce).

S11 GENERALIZES the route-contract solution to inter-module Python symbols:

  S11.1 the exporter's symbol contract is engine-declared DATA, materialised
        at plan time for every dependency pair (typed needs edges; the S10.22
        collapsed plan: core imports each pinned leaf) and persisted to
        ``contracts/modules.json`` from the same in-memory datum every
        consumer reads. Sources in priority order: the exporter's ``exposes``
        entries ('name(args)' shapes); else a deterministic derivation from
        the exporter's OWN requirement/spec text and the constitution
        (``<stem>.<name>`` references — the same way route bindings derive
        from human text); else the EMPTY set.
  S11.2 BOTH bindings print the contract: the exporter's spec lists the exact
        symbols it MUST define; every importer's spec lists the ONLY symbols
        it may import — the same rendered datum, so prompt and gate cannot
        drift. An exporter with importers and an EMPTY contract reds honestly
        at the card gate demanding ``exposes``.
  S11.3 two delivery gates at the ONE write door (contract datum, NOT the
        live file — order-independent, closing the from-import exemption of
        S10.25): (a) the exporter must define ALL contracted symbols;
        (b) an importer may ``from X import Y`` / ``X.Y`` ONLY contracted
        symbols; ``from X import *`` on a contracted module is refused
        (it bypasses the pinned surface); bare ``import X`` stays clean.
        Modules with NO contract (stdlib, not-in-plan) are untouched.
  S11.4 doctor/rework re-prints the contract block in the repair directive,
        so a rewriting LLM sees the frozen surface.
"""
import json
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402

# the v157 contract shape: three storage symbols both sides must agree on
_DB_EXPOSES = ["init_db(path)", "store_note(text)", "list_notes()"]
_DB_SURFACE = "init_db(path), store_note(text), list_notes()"

# the REAL p6 constitution rules (verbatim class): rule 1 pins src/db.py,
# rule 3 names the ONLY storage symbol the human ever stated — db.connect
_PIN_RULE = ("Standard library ONLY: HTTP through a WSGI app (src/app.py "
             "exposes wsgi_app), storage through sqlite3 in src/db.py. "
             "No third-party packages.")
_CONNECT_RULE = ("Every test sets the NOTES_DB env var to a fresh temp path "
                 "BEFORE connecting (db.connect reads it per call).")


def _engine(tmp_path, constitution=None):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e.workspace.open()
    e._constitution = list(constitution or [])
    e._product_contract = lambda: {"entry": "src/app.py", "boot": {},
                                   "routes": []}
    return e


def _register(e, importer="core", exposes=_DB_EXPOSES, requirement=""):
    db = {"id": "db", "title": "sqlite storage layer"}
    if exposes:
        db["exposes"] = list(exposes)
    if requirement:
        db["requirement"] = requirement
    e._register_module_import(importer, db)
    return db


def _refusals(ws, rel):
    return [a for a in ws.artifacts
            if a.get("path") == rel and str(a.get("type", "")).startswith(
                "refused_")]


# ── S11.1 plan-time materialization: exposes → contracts/modules.json ────────

def test_materializes_contract_from_exposes(tmp_path):
    e = _engine(tmp_path)
    _register(e)
    mj = json.loads((tmp_path / "wk" / "contracts" / "modules.json")
                    .read_text(encoding="utf-8"))
    assert mj.get("db") == [
        {"name": "init_db", "args": ["path"]},
        {"name": "store_note", "args": ["text"]},
        {"name": "list_notes", "args": []},
    ], ("the exporter's exposes entries must materialise as the module "
        f"symbol contract datum in contracts/modules.json; got {mj!r}")


def test_derives_contract_from_own_text_when_exposes_absent(tmp_path):
    # the honest fallback is DERIVATION from the exporter's own human data —
    # never a domain default. The real p6 constitution states db.connect.
    e = _engine(tmp_path, constitution=[_PIN_RULE, _CONNECT_RULE])
    _register(e, exposes=None,
              requirement="Build src/db.py exactly as the constitution "
                          "mandates — " + _PIN_RULE)
    mj = json.loads((tmp_path / "wk" / "contracts" / "modules.json")
                    .read_text(encoding="utf-8"))
    names = [x.get("name") for x in mj.get("db", [])]
    assert names == ["connect"], (
        "with no exposes the contract derives from the exporter's own "
        "requirement + constitution (db.connect — the only human-stated "
        f"storage symbol); got {mj!r}")
    assert "py" not in names, "src/db.py filename mentions are NOT symbols"


def test_empty_contract_is_honest_red_at_card_gate(tmp_path):
    # no exposes, nothing derivable → EMPTY contract; the exporter's card
    # reds demanding exposes (never a silent guess, never a domain default)
    e = _engine(tmp_path, constitution=["No third-party packages."])
    db = _register(e, exposes=None)
    found = [f for f in e._card_completeness_findings(db)
             if "exposes" in f and "src/db.py" in f]
    assert found, (
        "an exporter that importers depend on, with NO contracted symbols, "
        "must red at the card gate demanding `exposes` — got findings: "
        f"{e._card_completeness_findings(db)!r}")
    # and the importer's binding forbids importing from it
    core = {"id": "core", "title": "product core"}
    binding = e._module_contract_binding(core)
    assert "db" in binding and "not import" in binding.lower(), (
        "an importer of an uncontracted module must be told NOT to import "
        f"from it; got {binding!r}")


# ── S11.2 both bindings print the SAME contract datum ─────────────────────────

def test_both_bindings_carry_identical_surface(tmp_path):
    e = _engine(tmp_path)
    db = _register(e)
    exp = e._module_contract_binding(db)
    imp = e._module_contract_binding({"id": "core", "title": "product core"})
    assert _DB_SURFACE in exp, (
        "the EXPORTER binding must order the exact contracted signatures; "
        f"got {exp!r}")
    assert _DB_SURFACE in imp, (
        "the IMPORTER binding must list the ONLY importable symbols — the "
        f"same rendered datum; got {imp!r}")
    assert "MUST define" in exp and "ONLY" in imp, (
        "each side gets its OWN obligation over the shared surface")


def test_no_contract_module_gets_no_binding(tmp_path):
    e = _engine(tmp_path)
    _register(e)
    stranger = {"id": "web_ui", "title": "web ui"}
    assert e._module_contract_binding(stranger) == "", (
        "a node in NO dependency pair gets no module-contract block — the "
        "datum never invents obligations")


# ── S11.3 delivery gates at the ONE write door (contract datum, not files) ────

_DB_OK = ("def init_db(path):\n    return path\n\n\n"
          "def store_note(text):\n    return 1\n\n\n"
          "def list_notes():\n    return []\n")

# the v156/v157 exporter shape: right module, DIFFERENT invented names
_DB_WRONG = ("def insert_note(text):\n    return 1\n\n\n"
             "def get_notes():\n    return []\n")

# v157 workspace/src/core.py line 3 — literal reproduction
_V157_CORE_IMPORT = "from db import init_db, store_note, list_notes\n"


def test_exporter_missing_contracted_symbol_is_refused(tmp_path):
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("src/db.py", _DB_WRONG, "code")
    ref = _refusals(ws, "src/db.py")
    assert ref, ("an exporter delivery missing contracted symbols must be "
                 "REFUSED at the write door (v157: db.py shipped different "
                 "names and the collision surfaced only at assembly)")
    reason = ref[0].get("reason", "")
    for missing in ("init_db", "store_note", "list_notes"):
        assert missing in reason, (
            f"the refusal must NAME the missing symbol {missing}; got "
            f"{reason!r}")
    assert not (tmp_path / "wk" / "src" / "db.py").exists(), (
        "a refused exporter delivery must never land")
    # green twin: the conforming exporter lands cleanly
    ws._write("src/db.py", _DB_OK, "code")
    assert (tmp_path / "wk" / "src" / "db.py").is_file()
    assert len(_refusals(ws, "src/db.py")) == 1


def test_importer_from_import_outside_contract_is_refused(tmp_path):
    # ORDER-INDEPENDENT: src/db.py does NOT exist on disk — the gate reads
    # the CONTRACT datum, closing the from-import exemption of S10.25
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("src/core.py",
              "from db import store_note, drop_all\n\n\n"
              "def post_notes(payload, query):\n"
              "    return 201, {\"id\": store_note(payload)}\n", "code")
    ref = _refusals(ws, "src/core.py")
    assert ref, ("`from X import Y` with Y outside X's symbol contract must "
                 "be refused at the write door even when src/X.py is not "
                 "built yet (build order must not matter)")
    reason = ref[0].get("reason", "")
    assert "drop_all" in reason and _DB_SURFACE in reason, (
        "the refusal must name the phantom symbol AND print the contracted "
        f"surface; got {reason!r}")
    # green twin: contracted symbols import cleanly (still no db.py on disk)
    ws._write("src/core.py",
              "from db import init_db, store_note\n\n\n"
              "def post_notes(payload, query):\n"
              "    return 201, {\"id\": store_note(payload)}\n", "code")
    assert (tmp_path / "wk" / "src" / "core.py").is_file()


def test_importer_attr_use_outside_contract_is_refused(tmp_path):
    # the attr form checked against the DATUM (S10.25 needs the live file;
    # the contract gate must not) — src/db.py absent on purpose
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("tests/test_core.py",
              "import db\n\n\ndef test_wipe():\n    db.wipe()\n", "test")
    ref = _refusals(ws, "tests/test_core.py")
    assert ref and "wipe" in ref[0].get("reason", ""), (
        "`X.attr` outside X's contract must be refused in tests/ too, "
        f"file-independent; got {ref!r}")
    # green: contracted attr call + bare import land
    ws._write("tests/test_core.py",
              "import db\n\n\ndef test_list():\n    db.list_notes()\n",
              "test")
    assert (tmp_path / "wk" / "tests" / "test_core.py").is_file()


def test_star_import_of_contracted_module_is_refused(tmp_path):
    # documented behavior: `from X import *` bypasses the pinned surface —
    # refused for a CONTRACTED module; bare `import X` with no attr use is
    # clean (nothing to check)
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("src/core.py", "from db import *\n", "code")
    assert _refusals(ws, "src/core.py"), (
        "star-import evades the symbol contract and must be refused")
    ws._write("src/web_ui.py", "import db\n\n\nX = 1\n", "code")
    assert (tmp_path / "wk" / "src" / "web_ui.py").is_file(), (
        "bare `import X` uses no symbol — it must land")


def test_uncontracted_modules_stay_untouched(tmp_path):
    # green edges: stdlib and not-in-plan modules are NOT the datum's
    # business — S10.25 (live-file phantom scan) still owns those seams
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("src/core.py",
              "import json\nfrom helpers import anything_at_all\n\n\n"
              "def get_notes(payload, query):\n"
              "    return 200, json.dumps([])\n", "code")
    assert (tmp_path / "wk" / "src" / "core.py").is_file(), (
        "modules with NO contract (stdlib, not-in-plan) must land untouched")


def test_import_from_empty_contract_module_is_refused(tmp_path):
    # the honest-red path end-to-end: nothing derivable → EMPTY contract →
    # ANY import from the module is refused, pointing at the missing exposes
    e = _engine(tmp_path, constitution=["No third-party packages."])
    _register(e, exposes=None)
    ws = e.workspace
    ws._write("src/core.py", "from db import connect\n", "code")
    ref = _refusals(ws, "src/core.py")
    assert ref and "exposes" in ref[0].get("reason", ""), (
        "importing from a module whose contract is EMPTY must be refused "
        f"demanding declared exposes; got {ref!r}")


def test_v157_collision_is_impossible_at_delivery(tmp_path):
    """v157 literal: core.py 'from db import init_db, store_note, list_notes'
    + db.py with different names must be IMPOSSIBLE to land — one side reds
    at delivery, never at assembly."""
    e = _engine(tmp_path)
    _register(e)                             # contract = the three symbols
    ws = e.workspace
    # core's import agrees with the CONTRACT → lands (waiting for db)
    ws._write("src/core.py", _V157_CORE_IMPORT + "\n\n"
              "def post_notes(payload, query):\n"
              "    return 201, {\"id\": store_note(payload)}\n", "code")
    assert (tmp_path / "wk" / "src" / "core.py").is_file()
    # db's rework ships the OTHER guess → refused at ITS delivery
    ws._write("src/db.py", _DB_WRONG, "code")
    assert _refusals(ws, "src/db.py"), (
        "the v157 collision reached assembly as an ImportError at boot — "
        "the exporter side must red at delivery instead")
    # and the conforming db lands: the pair is coherent BY CONSTRUCTION
    ws._write("src/db.py", _DB_OK, "code")
    assert (tmp_path / "wk" / "src" / "db.py").is_file()


# ── S11.4 doctor/rework re-prints the frozen surface ─────────────────────────

def test_rework_directive_reprints_contract_block(tmp_path):
    # v157: the doctor reworked core three times and each rewrite re-guessed
    # the db surface — the repair directive must re-print the contract block
    e = _engine(tmp_path)
    _register(e)
    ws = e.workspace
    ws._write("specs/core.md", "# core\nold spec without the contract\n",
              "spec")
    e.agents = dict(getattr(e, "agents", None) or {})
    e.agents["implementer"] = lambda ctx: None
    assert e._remedy_rework_module("core", "GET /notes -> 500")
    text = (tmp_path / "wk" / "specs" / "core.md").read_text(
        encoding="utf-8")
    assert _DB_SURFACE in text, (
        "the repair directive must re-print the module symbol contract so "
        "the rewriting LLM sees the frozen surface — spec after rework: "
        f"{text!r}")


# ── S11 dynamic: offline run, both bindings carry the same contract ─────────

_POLICY = {"measurable_target": True, "spend_per_action_usd": 0,
           "human_in_loop": True, "involves_outreach": False,
           "consent_obtained": True, "legal_exposure": False,
           "legality_reviewed": True}

_PINNED_PROJECT = {
    "name": "micro-notes-symbols",
    "goal": ("A tiny notes service over WSGI: POST /notes stores {text} and"
             " returns {id}; GET /notes returns the items. src/app.py"
             " exposes wsgi_app."),
    "target": "POST then GET round-trips a note; sqlite3 stdlib only",
    "constitution": [_PIN_RULE, _CONNECT_RULE],
    "acceptance": {"smoke": ["the build succeeds"]},
    "policy": dict(_POLICY),
}

_BIG = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 300,
        "open_decisions": 0, "single_concern": False,
        "testable_criteria": True}
_SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
          "open_decisions": 0, "single_concern": True,
          "testable_criteria": True}


def _split_decomposer(ctx):
    # the v155/v157 shape: a 2-child plan the small-product floor rejects
    if ctx["depth"] == 0:
        return {"atomic": False, "metrics": dict(_BIG),
                "spec_markdown": ("## Scope\nIn:\n"
                                  "- src/db.py: sqlite storage\n"
                                  "- src/app.py: wsgi_app dispatch\n"),
                "children": [{"id": "db_persistence",
                              "title": "sqlite storage layer"},
                             {"id": "wsgi_handler",
                              "title": "WSGI route handlers"}]}
    return {"metrics": dict(_SMALL),
            "acceptance": ["Given a note, When POSTed to /notes, Then GET"
                           " /notes returns it"]}


def _conforming_implementer(ctx):
    ws, node = ctx["workspace"], ctx["node"]
    if node == "db":
        ws._write("src/db.py",
                  "import os\nimport sqlite3\n\n\ndef connect():\n"
                  "    return sqlite3.connect(os.environ['NOTES_DB'])\n",
                  "code")
    else:
        ws._write(f"src/{node}.py",
                  "import db\n\n\ndef post_notes(payload, query):\n"
                  "    db.connect()\n    return 201, {\"id\": 1}\n\n\n"
                  "def get_notes(payload, query):\n"
                  "    db.connect()\n    return 200, {\"items\": []}\n",
                  "code")
    ws._write(f"tests/test_{node}.py",
              f"def test_{node}_placeholder():\n    assert True\n", "test")


def _v157_implementer(ctx):
    ws, node = ctx["workspace"], ctx["node"]
    if node == "db":
        ws._write("src/db.py", _DB_WRONG, "code")     # invented names
    else:
        ws._write(f"src/{node}.py",
                  _V157_CORE_IMPORT +                 # the literal line
                  "\n\ndef post_notes(payload, query):\n"
                  "    return 201, {\"id\": store_note(payload)}\n", "code")


def test_dynamic_collapse_bindings_share_contract(plugin, tmp_path):
    res = eng.run_project(dict(_PINNED_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools,
                          contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _split_decomposer,
                                  "implementer": _conforming_implementer})
    assert res is not None
    mj = json.loads((tmp_path / "wk" / "contracts" / "modules.json")
                    .read_text(encoding="utf-8"))
    assert [x.get("name") for x in mj.get("db", [])] == ["connect"], (
        "the collapsed plan (core imports pinned db) must materialise db's "
        f"contract from the constitution's db.connect; got {mj!r}")
    specs = {p.name: p.read_text(encoding="utf-8")
             for p in sorted((tmp_path / "wk" / "specs").glob("*.md"))}
    db_spec = specs.get("db.md", "")
    core_spec = specs.get("core.md", "")
    assert "connect(...)" in db_spec and "MUST define" in db_spec, (
        "the EXPORTER spec must carry the engine-declared symbol contract; "
        f"got {db_spec!r}")
    assert "connect(...)" in core_spec and "ONLY" in core_spec, (
        "the IMPORTER spec must carry the SAME rendered surface; got "
        f"{core_spec!r}")
    assert (tmp_path / "wk" / "src" / "db.py").is_file(), (
        "a conforming exporter must land cleanly (green edge)")


def test_dynamic_v157_collision_never_lands(plugin, tmp_path):
    # both sides re-guess the surface exactly as v157 did — with the datum
    # the collision is refused at DELIVERY and can never reach assembly
    res = eng.run_project(dict(_PINNED_PROJECT),
                          workspace=str(tmp_path / "wk"), depth="product",
                          tools=plugin.tools,
                          contracts_dir=str(eng.CONTRACTS),
                          agents={"decomposer": _split_decomposer,
                                  "implementer": _v157_implementer})
    assert res is not None
    landed = {p.name: p.read_text(encoding="utf-8")
              for p in (tmp_path / "wk" / "src").glob("*.py")
              if p.is_file()} if (tmp_path / "wk" / "src").is_dir() else {}
    for name, text in landed.items():
        assert _V157_CORE_IMPORT.strip() not in text, (
            f"the v157 phantom import LANDED in src/{name} — the importer "
            "gate must refuse it at the write door")
    db_text = landed.get("db.py", "")
    assert "insert_note" not in db_text and "get_notes" not in db_text, (
        "db.py with invented names landed — the exporter gate must refuse "
        "a delivery missing the contracted symbols")
