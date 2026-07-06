"""Audit STAGE 36 — a NON-HTTP node's specs/*.md is COMPILED FROM the machine
behaviour carrier (Gherkin ``behavior`` + typed ``symbols.exposes``), prose is
derived (node N4, the format-flip finale for the non-HTTP class).

Root failure this pins (the K3/S23 blind spot): K3 flipped the format ONLY for
HTTP nodes — a node carrying an OpenAPI document renders a
``## Interface (compiled from the machine OpenAPI)`` section straight from the
machine artifact. But a NON-HTTP code leaf (storage/lib, the v166 db_layer
class) owns NO OpenAPI. N3/S32 gave that class its OWN machine carrier —
``node["behavior"]`` (a Gherkin feature) plus typed ``node["symbols"]["exposes"]``
(each callable with arg types, return type and an error surface). Yet the .md
for such a node is still written ONLY from the decomposer's PROSE
(``node["spec_markdown"]``): the machine behaviour carrier that N3 validates
never reaches the human spec deterministically. Behaviour and API drift away
from the machine artifacts exactly the way get_ping drifted as prose in v165.

Target of the flip (N3 already made the Gherkin+symbols the machine carrier for
this class): the human-readable specs/*.md for a non-HTTP node must be
DETERMINISTICALLY COMPILED FROM that carrier, so that
  (S36.1) a node carrying a Gherkin ``behavior`` feature renders its behaviour
          (each Scenario's Given/When/Then) into the .md straight from the
          feature text — the same feature always yields the same section;
  (S36.2) a node carrying typed ``symbols.exposes`` renders an API section —
          each callable's signature (name, typed args, return type, declared
          raises) — straight from the symbols, no prose guess;
  (S36.3) both sections are a FUNCTION OF THE CARRIER ONLY: two different
          decomposer proses over the SAME behaviour+symbols produce a
          byte-identical Behaviour section and a byte-identical API section
          (prose is no longer the behaviour/API source — editing the prose
          cannot change what the build reads);
  (S36.4) a node with NEITHER a ``behavior`` feature NOR ``symbols.exposes``
          (a bare node) grows no phantom Behaviour/API section — historical
          output is preserved, no false section.

These reds prove the CURRENT engine ignores the machine behaviour carrier when
writing a non-HTTP node's .md (the behaviour/API live only in prose). The fix
compiles the .md FROM the carrier in the engine — never in the reviewer, never
in the case; and it leaves the human prose as a strictly SECONDARY block that
does not feed the build.
"""
import pathlib
import re
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402


_BEHAVIOUR_HEAD = "## Behaviour (compiled from the machine Gherkin)"
_API_HEAD = "## API (compiled from symbols)"


def _bare_engine(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._product_contract = lambda: {"entry": "src/app.py", "boot": {},
                                   "routes": []}
    return e


def _db_layer_carrier():
    """The COMPLETE non-HTTP machine behaviour carrier N3/S32 validates: a
    Gherkin ``behavior`` feature plus typed ``symbols.exposes`` with return
    types and a declared error surface — the exact shape the decomposer emits
    for a storage/lib leaf. This is the machine artifact the .md must derive
    from (mirrors tests/audit/test_decomposer_emits_gherkin's fixture)."""
    feature = "\n".join([
        "Feature: db_layer sqlite storage",
        "  Scenario: connect opens a database handle",
        "    Given a filesystem path 'notes.db'",
        "    When connect(path) is called",
        "    Then a sqlite3.Connection is returned",
        "  Scenario: insert_note stores a note and returns its id",
        "    Given an open connection and body 'hello'",
        "    When insert_note(conn, body) is called",
        "    Then the new integer row id is returned",
        "",
    ])
    exposes = [
        {"name": "connect", "args": ["path: str"],
         "returns": "sqlite3.Connection", "raises": ["sqlite3.OperationalError"]},
        {"name": "insert_note",
         "args": ["conn: sqlite3.Connection", "body: str"],
         "returns": "int", "raises": ["sqlite3.IntegrityError"]},
    ]
    return {"id": "db_layer", "files": ["src/db_layer.py"],
            "behavior": feature, "symbols": {"exposes": exposes}}


def _section(md: str, head: str) -> str:
    """The block a compiled section owns: the heading line plus every following
    line until the next ``## `` heading. Empty string when the section is
    absent — so a phantom/empty section is distinguishable from a real one."""
    lines = md.splitlines()
    out = []
    grabbing = False
    for ln in lines:
        if ln.strip() == head:
            grabbing = True
            out.append(ln.rstrip())
            continue
        if grabbing:
            if ln.startswith("## ") and ln.strip() != head:
                break
            out.append(ln.rstrip())
    return "\n".join(out).strip()


# ── S36.1 the .md renders behaviour FROM the machine Gherkin feature ──────────

def test_spec_md_renders_behaviour_from_machine_gherkin(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    node = _db_layer_carrier()
    e.workspace.spec("db_layer", "sqlite storage", 1, "leaf", "", "L0", [],
                     node=node)
    md = (tmp_path / "wk" / "specs" / "db_layer.md").read_text(encoding="utf-8")
    assert _BEHAVIOUR_HEAD in md, (
        "specs/db_layer.md has no '%s' section — a non-HTTP node's behaviour is"
        " NOT compiled FROM the machine Gherkin carrier (it only ever reached"
        " the .md as decomposer prose, the K3 blind spot for the non-HTTP class)"
        % _BEHAVIOUR_HEAD)
    beh = _section(md, _BEHAVIOUR_HEAD)
    # every Scenario name and its Given/When/Then steps the feature declares
    # must be readable in the human .md, straight from the feature text.
    for needle in ("connect opens a database handle",
                   "When connect(path) is called",
                   "Then a sqlite3.Connection is returned",
                   "insert_note stores a note and returns its id"):
        assert needle in beh, (
            "the machine Gherkin feature's %r is absent from the compiled"
            " Behaviour section — the .md is not derived FROM the feature"
            % needle)


# ── S36.2 the .md renders the API FROM typed symbols.exposes ──────────────────

def test_spec_md_renders_api_from_symbols(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    node = _db_layer_carrier()
    e.workspace.spec("db_layer", "sqlite storage", 1, "leaf", "", "L0", [],
                     node=node)
    md = (tmp_path / "wk" / "specs" / "db_layer.md").read_text(encoding="utf-8")
    assert _API_HEAD in md, (
        "specs/db_layer.md has no '%s' section — the typed symbols.exposes"
        " carrier is not compiled into the human spec; a weak LLM must guess"
        " every signature (the exact v166 db_layer hole)" % _API_HEAD)
    api = _section(md, _API_HEAD)
    # each callable's full typed signature is DATA in symbols.exposes — it must
    # be readable in the human spec: name, typed args, return type, raises.
    for needle in ("connect", "path: str", "sqlite3.Connection",
                   "sqlite3.OperationalError", "insert_note",
                   "conn: sqlite3.Connection", "body: str", "int",
                   "sqlite3.IntegrityError"):
        assert needle in api, (
            "the symbols.exposes datum %r is absent from the compiled API"
            " section — the signature is left implicit for a later prose guess"
            % needle)


# ── S36.3 behaviour+API are a FUNCTION OF THE CARRIER ONLY (prose is derived) ─

def test_sections_are_invariant_under_prose_edits(tmp_path):
    node = _db_layer_carrier()

    def _write_with_prose(prose, sub):
        e = _bare_engine(tmp_path / sub)
        e.workspace.open()
        n = dict(node, spec_markdown=prose)
        e.workspace.spec("db_layer", "sqlite storage", 1, "leaf", "", "L0", [],
                         node=n)
        return (tmp_path / sub / "wk" / "specs" / "db_layer.md").read_text(
            encoding="utf-8")

    md_a = _write_with_prose(
        "## Notes\n- connect returns a handle; it can raise anything.", "a")
    md_b = _write_with_prose(
        "## Totally different\n- Nothing here names a callable or a step.", "b")

    beh_a, beh_b = _section(md_a, _BEHAVIOUR_HEAD), _section(md_b, _BEHAVIOUR_HEAD)
    api_a, api_b = _section(md_a, _API_HEAD), _section(md_b, _API_HEAD)
    assert beh_a and api_a, (
        "no Behaviour/API section was compiled from the carrier at all — the"
        " .md carries them only when prose happens to mention them")
    assert beh_a == beh_b, (
        "the Behaviour section changed when only the decomposer PROSE changed —"
        " behaviour is still sourced FROM the prose, not from the machine"
        " Gherkin feature. Same feature must yield a byte-identical section"
        " (N4: prose is derived, editing it must not change the build input)."
        "\n--- A ---\n%s\n--- B ---\n%s" % (beh_a, beh_b))
    assert api_a == api_b, (
        "the API section changed when only the decomposer PROSE changed — the"
        " API is still sourced FROM the prose, not from typed symbols.exposes."
        "\n--- A ---\n%s\n--- B ---\n%s" % (api_a, api_b))


# ── S36.4 no carrier → no phantom sections (historical output kept) ───────────

def test_node_without_carrier_grows_no_sections(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    # a bare node: no openapi, no behavior feature, no symbols.exposes — only
    # human prose. It must grow NEITHER a Behaviour nor an API section.
    e.workspace.spec("readme_only", "context node", 1, "leaf", "", "L0", [],
                     node={"id": "readme_only",
                           "spec_markdown": "## Scope\n- Just a note."})
    md = (tmp_path / "wk" / "specs" / "readme_only.md").read_text(
        encoding="utf-8")
    assert _section(md, _BEHAVIOUR_HEAD) == "", (
        "a node with no Gherkin behaviour carrier grew a Behaviour section —"
        " the compiler must stay silent when the node carries no feature")
    assert _section(md, _API_HEAD) == "", (
        "a node with no symbols.exposes grew an API section — the compiler must"
        " stay silent when the node exposes no typed callables")


# ── S36.5 the prose remains a SECONDARY block, below the compiled sections ────

def test_prose_stays_secondary_below_compiled_sections(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    node = dict(_db_layer_carrier(),
                spec_markdown="## Human context\n- extra reader notes.")
    e.workspace.spec("db_layer", "sqlite storage", 1, "leaf", "", "L0", [],
                     node=node)
    md = (tmp_path / "wk" / "specs" / "db_layer.md").read_text(encoding="utf-8")
    # the machine-compiled sections must precede the human prose block: the
    # carrier is the source, the prose is a downstream reader.
    i_beh = md.find(_BEHAVIOUR_HEAD)
    i_api = md.find(_API_HEAD)
    i_prose = md.find("## Human context")
    assert i_beh != -1 and i_api != -1 and i_prose != -1
    assert i_beh < i_prose and i_api < i_prose, (
        "the human prose block appears BEFORE the machine-compiled Behaviour/"
        " API sections — the derived-from-carrier sections must come first so"
        " the prose reads as a secondary downstream block, not the source")
