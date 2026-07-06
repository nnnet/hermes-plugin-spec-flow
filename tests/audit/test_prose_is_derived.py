"""Audit STAGE 23 — specs/*.md is COMPILED FROM the machine OpenAPI, prose is
not a source (K3, the format-flip finale).

Root failure this pins (p6-micro-notes, v165): a node's interface — the routes
it owns and their handlers — reached the written spec ONLY as decomposer PROSE
(``node["spec_markdown"]`` → ``specs/<nid>.md``). A route injected as prose
(``GET /ping -> "pong"``) drifted away from the engine's machine artifacts and
``get_ping`` was lost. The spec .md was a CARRIER of the interface command, not
a DERIVED reader of it.

Target of the flip (E1/K2 already made the machine OpenAPI primary — it lives
at ``ir["nodes"][<nid>]["openapi"]``): the human-readable specs/*.md must be
DETERMINISTICALLY COMPILED FROM that machine OpenAPI document, so that
  (S23.1) a node carrying an OpenAPI document renders its interface (every
          ``METHOD /path`` it owns, and each route's handler symbol) into the
          .md straight from that document — the same document always yields the
          same interface section;
  (S23.2) the interface section is a FUNCTION OF THE OpenAPI ONLY: two
          different decomposer proses over the SAME OpenAPI produce a
          byte-identical interface section (prose is no longer the interface
          source — editing the prose cannot change what the build reads);
  (S23.3) a node with NO OpenAPI (a non-service leaf) grows no phantom/empty
          interface section — historical output is preserved.

These reds prove the CURRENT engine ignores the machine OpenAPI when writing
the .md (the interface lives only in prose). The fix compiles the .md FROM the
OpenAPI in the engine — never in the reviewer, never in the case.
"""
import pathlib
import re
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402


def _bare_engine(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._product_contract = lambda: {"entry": "src/app.py", "boot": {},
                                   "routes": []}
    return e


def _node_openapi_doc(nid, routes, reqf=None, media=None):
    """A REAL OpenAPI 3.1 document for the routes a node OWNS, built the exact
    way the engine builds ir[...][openapi] (spec_ir._node_openapi over the pure
    route helpers). This is the machine artifact the .md must be derived from."""
    return spec_ir._node_openapi(
        nid, routes, reqf or {}, media or {},
        eng._route_success_status, eng._route_fixed_body,
        eng._canonical_handler_symbol)


# A STRUCTURED interface entry the engine compiles for a route: a bulleted
# ``METHOD /path`` emphasised as a heading (``**GET /ping**``), optionally
# bound to a handler. Free prose that merely names a route inside a sentence
# ("pings back on GET /ping") never matches — so this captures the machine-
# sourced interface, not incidental mentions.
_IFACE_ENTRY = re.compile(
    r"^\s*[-*]\s*\*\*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/\S*)\*\*")


def _interface_section(md: str) -> str:
    """The machine-sourced interface facts carried by the .md: every structured
    ``**METHOD /path**`` entry (with its handler/status/field detail lines).
    This block must be a pure function of the node's OpenAPI document. Empty
    string when the .md exposes no structured interface at all."""
    facts = []
    for ln in md.splitlines():
        if _IFACE_ENTRY.match(ln):
            facts.append(ln.rstrip())
        elif facts and re.match(r"^\s+[-*]\s", ln):
            # a detail line (handler/status/fields) attached to the last entry
            facts.append(ln.rstrip())
    return "\n".join(facts)


# ── S23.1 the .md renders the interface FROM the machine OpenAPI ──────────────

def test_spec_md_renders_interface_from_machine_openapi(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    routes = [("GET", "/ping"), ("POST", "/notes")]
    doc = _node_openapi_doc("core", routes, reqf={("POST", "/notes"): ["text"]})
    e.workspace.spec("core", "notes core", 1, "leaf", "", "L0", [],
                     node={"id": "core", "openapi": doc})
    md = (tmp_path / "wk" / "specs" / "core.md").read_text(encoding="utf-8")
    # every route the machine OpenAPI OWNS must appear in the human .md, and it
    # must come FROM the OpenAPI (the node carried NO prose interface at all).
    assert "GET /ping" in md, (
        "specs/core.md does not render the machine OpenAPI's GET /ping — the"
        " .md is not compiled FROM the node's OpenAPI document (v165: the"
        " interface only reached the .md as decomposer prose)")
    assert "POST /notes" in md, (
        "specs/core.md does not render the machine OpenAPI's POST /notes")
    # the handler symbol the OpenAPI declares is DATA — it must be readable in
    # the human spec too, not left implicit for a later prose guess.
    handler = doc["paths"]["/ping"]["get"]["x-spec-flow-handler"]
    assert handler and handler in md, (
        "the OpenAPI's declared handler %r for GET /ping is absent from the"
        " compiled .md" % handler)


# ── S23.2 the interface is a FUNCTION OF THE OpenAPI ONLY (prose is derived) ──

def test_interface_section_is_invariant_under_prose_edits(tmp_path):
    routes = [("GET", "/ping")]
    doc = _node_openapi_doc("core", routes)

    def _write_with_prose(prose, sub):
        e = _bare_engine(tmp_path / sub)
        e.workspace.open()
        e.workspace.spec("core", "notes core", 1, "leaf", "", "L0", [],
                         node={"id": "core", "openapi": doc,
                               "spec_markdown": prose})
        return (tmp_path / sub / "wk" / "specs" / "core.md").read_text(
            encoding="utf-8")

    md_a = _write_with_prose(
        "## Notes\n- The engine pings back with pong on GET /ping.", "a")
    md_b = _write_with_prose(
        "## Totally different prose\n- Nothing here mentions any route.", "b")

    sec_a, sec_b = _interface_section(md_a), _interface_section(md_b)
    assert sec_a, (
        "no interface section was compiled from the OpenAPI at all — the .md"
        " carries the interface only when prose happens to mention it")
    assert sec_a == sec_b, (
        "the .md interface section changed when only the decomposer PROSE"
        " changed — the interface is still sourced FROM the prose, not from the"
        " machine OpenAPI. Same OpenAPI must yield a byte-identical interface"
        " section (K3: prose is derived, editing it must not change the build"
        " input).\n--- prose A ---\n%s\n--- prose B ---\n%s" % (sec_a, sec_b))


# ── S23.3 no OpenAPI → no phantom interface section (historical output kept) ──

def test_node_without_openapi_grows_no_interface_section(tmp_path):
    e = _bare_engine(tmp_path)
    e.workspace.open()
    e.workspace.spec("lib_util", "pure helper", 1, "leaf", "", "L0", [],
                     node={"id": "lib_util",
                           "spec_markdown": "## Scope\n- A pure string helper."})
    md = (tmp_path / "wk" / "specs" / "lib_util.md").read_text(encoding="utf-8")
    assert _interface_section(md) == "", (
        "a node with no OpenAPI document grew a route interface section — the"
        " compiler must stay silent when the node owns no routes")
