"""The node-detail panel is HONEST: latest-verdict problems, machine spec only.

Why (user 2026-07-07, HARD constraints for dashboard-C7): the node panel showed
a contradictory, stale view — STALE gate FAILs listed as open even though a LATER
verdict for the same gate on the same node was PASS; a carrier-less node rendered
NOTHING useful (a soft "not yet dumped" note) and then fell through to `.md`
PROSE as if prose were the spec. The three HARD rules this test pins:

  1. NO fallback to prose — the panel shows the MACHINE carrier (OpenAPI /
     Gherkin / typed symbols) from ir.json, never `specs/*.md` prose.
  2. A missing machine carrier is an ERROR shown honestly (red), never hidden,
     never faked green, never filled with prose.
  3. Latest verdict wins — a gate that FAILed then PASSed is PASS, not an open
     problem (read verdicts from trace.jsonl in order, per node+gate).

What: builds a SELF-CONTAINED fake run dir (deterministic, no LLM) with a
trace where node `alpha`'s `spec_lint` gate FAILs (tick 10) then PASSes (tick 12),
and an ir.json where `alpha` HAS a machine carrier (OpenAPI paths) while `beta`
has NONE. Asserts: the FAIL->PASS gate is not an unclosed problem; the
carrier-less node is reported as an error (no spec), not prose, not green; and
no prose from `specs/*.md` reaches the rendered machine spec.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402

# A distinctive prose sentence that lives ONLY in specs/*.md — it must NEVER
# appear in any machine-spec rendering (that would be prose-as-spec).
_PROSE_MARKER = "THIS-IS-PROSE-FROM-MARKDOWN-NOT-A-SPEC"

# alpha: an HTTP node WITH a machine carrier (a non-empty OpenAPI paths map).
_ALPHA_IR = {
    "openapi": {
        "openapi": "3.1.0",
        "info": {"title": "alpha", "version": "1"},
        "paths": {
            "/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
    },
}
# beta: a finished leaf with NO machine carrier at all (no openapi paths, no
# scenarios, no symbols) — a terminal ERROR state, not prose, not green.
_BETA_IR = {"openapi": {"openapi": "3.1.0", "info": {"title": "beta",
                                                     "version": "1"},
                        "paths": {}}}


def _fake_run(tmp_path):
    """A deterministic run dir: trace.jsonl + ir.json + tree.json + specs/*.md."""
    rd = tmp_path / "run"
    ws = rd / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    # trace: alpha's spec_lint FAILs then PASSes (same gate, later tick) plus a
    # downstream spec_review PASS; beta reaches a finished leaf with no carrier.
    trace = [
        {"tick": 5, "phase": "decompose", "task": "L0", "action": "root"},
        {"tick": 10, "phase": "review", "task": "alpha", "gate": "spec_lint",
         "verdict": "FAIL", "detail": "leaf card missing acceptance"},
        {"tick": 12, "phase": "review", "task": "alpha", "gate": "spec_lint",
         "verdict": "PASS", "detail": "reworked"},
        {"tick": 13, "phase": "review", "task": "alpha", "gate": "spec_review",
         "verdict": "PASS", "detail": "clean"},
        {"tick": 20, "phase": "review", "task": "beta", "gate": "spec_review",
         "verdict": "PASS", "detail": "clean"},
    ]
    (rd / "trace.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in trace) + "\n",
        encoding="utf-8")

    ir = {"format": "spec-ir/1", "product": {"kind": "service", "name": "x"},
          "nodes": {"alpha": _ALPHA_IR, "beta": _BETA_IR}}
    (ws / "ir.json").write_text(json.dumps(ir, ensure_ascii=False),
                                encoding="utf-8")

    tree = {"id": "L0", "verdict": "branch", "children": [
        {"id": "alpha", "verdict": "leaf", "children": []},
        {"id": "beta", "verdict": "leaf", "children": []}]}
    (rd / "tree.json").write_text(json.dumps(tree), encoding="utf-8")

    # prose specs — the .md a weak-model author wrote. NOT a spec.
    specs = rd / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "alpha.md").write_text("# alpha\n" + _PROSE_MARKER + "\n",
                                    encoding="utf-8")
    (specs / "beta.md").write_text("# beta\n" + _PROSE_MARKER + "\n",
                                   encoding="utf-8")
    return rd


# ── replicate the client evStatus so the "unclosed problems" list is testable ─

def _js_ev_status(e, all_events):
    """Mirror of the page's evStatus(): '' | 'open' | 'fixed' for one event."""
    bad = {"REJECT", "FAIL", "ERROR"}
    if str(e.get("verdict")) not in bad:
        return ""
    g = e.get("gate")
    t = e.get("tick") or 0

    def later_pass(gate):
        return any(x.get("gate") == gate and str(x.get("verdict")) == "PASS"
                   and (x.get("tick") or 0) > t for x in all_events)

    if g and later_pass(g):
        return "fixed"
    if str(g or "").startswith("spec") and later_pass("spec_review"):
        return "fixed"
    return "open"


# ── (3) latest verdict wins: FAIL->PASS gate is NOT an unclosed problem ───────

def test_fail_then_pass_gate_is_not_an_unclosed_problem(tmp_path):
    """HARD-3: alpha's spec_lint FAIL (tick 10) is closed by the same-gate PASS
    (tick 12); the panel's 'unclosed problems' list must be EMPTY for alpha, and
    its node badge must not carry an 'error' episode."""
    st = dash._build_state(_fake_run(tmp_path))
    alpha = st["nodes"]["alpha"]
    evs = alpha["events"]
    opened = [e for e in evs if _js_ev_status(e, evs) == "open"]
    assert not opened, (
        "stale FAIL listed as an unclosed problem despite a later same-gate "
        "PASS: %r" % opened)
    # the tree/badge episode must agree — no red 'error' glyph on a fixed node
    assert "error" not in alpha.get("episodes", []), (
        "node badge carries 'error' for a gate that FAILed then PASSed")


# ── (2) carrier-less node is an ERROR, never prose, never green ───────────────

def test_carrierless_node_is_reported_as_error(tmp_path):
    """HARD-2: beta has NO machine carrier -> its spec panel is an honest error
    block (red marker class), never a green validated badge."""
    st = dash._build_state(_fake_run(tmp_path))
    beta = st["nodes"]["beta"]
    std = beta.get("spec_standard", "")
    assert "specstd-missing" in std, (
        "carrier-less node does not render the honest no-machine-spec error")
    assert ("нет машинной спеки" in std or "no machine spec" in std.lower()), (
        "no-carrier error block does not state the missing machine spec")
    # not faked green: the M3 validated badge must be empty (nothing to green)
    assert not beta.get("spec_validated"), (
        "carrier-less node fakes a validated badge")
    assert "✓" not in std, "carrier-less node shows a green check"


def test_carrierless_node_spec_html_is_error_not_prose(tmp_path):
    """HARD-1/2: the machine-spec renderer returns the error block for a
    carrier-less node — never a soft 'not yet dumped' note, never prose."""
    html = dash._node_standard_spec_html("beta", dict(_BETA_IR))
    assert "specstd-missing" in html, (
        "carrier-less node's standard block is not the honest error")
    assert "ещё не выгружен" not in html or "не является" in html, (
        "carrier-less node still shows the old soft 'not yet dumped' note")


# ── (1) no prose from specs/*.md leaks into the machine spec ──────────────────

def test_no_prose_in_rendered_node_spec(tmp_path):
    """HARD-1: the server-rendered machine spec (spec_standard) of EITHER node
    must not contain prose lifted from specs/*.md — prose is not a spec."""
    st = dash._build_state(_fake_run(tmp_path))
    for nid in ("alpha", "beta"):
        std = st["nodes"][nid].get("spec_standard", "")
        assert _PROSE_MARKER not in std, (
            "prose from specs/%s.md leaked into the machine spec of %s" %
            (nid, nid))


def test_alpha_machine_spec_renders_the_carrier(tmp_path):
    """GREEN direction: alpha HAS a carrier, so its machine spec shows the
    OpenAPI route (the machine surface), not an error and not prose."""
    st = dash._build_state(_fake_run(tmp_path))
    alpha = st["nodes"]["alpha"]
    std = alpha.get("spec_standard", "")
    assert "/ping" in std, "alpha's machine spec omits its OpenAPI route"
    assert "specstd-missing" not in std, (
        "a node WITH a carrier is wrongly flagged as missing a machine spec")
    assert _PROSE_MARKER not in std, "prose leaked into alpha's machine spec"
