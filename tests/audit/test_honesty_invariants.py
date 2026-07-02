"""Audit Level B — honesty invariants of the engine's own machinery.

These are the properties that, if they ever break, let a hollow or dishonest
product pass as done. They run in milliseconds and gate the expensive run:
  * the engine-owned router carries real error branches, never a stub;
  * READY is a logical AND (a green base contract cannot lift a red project);
  * the card-completeness gate actually reds an under-specified exposing leaf.

Pure/deterministic: AST + string inspection + one cheap object, no LLM/HTTP.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


# ── the engine-owned router: no stubs, real error branches ───────────────────

_LEAVES = {
    "notes_post.py": (
        "def post_notes(payload, query):\n"
        "    text = (payload or {}).get('text')\n"
        "    if not text:\n        return (400, {'error': 'text required'})\n"
        "    return (201, {'id': 1})\n"),
    "notes_get.py": "def get_notes(payload, query):\n    return (200, {'items': []})\n",
    "ui.py": "def get_ui(payload, query):\n    return (200, '<html>ok</html>')\n",
}
_CONTRACT = {
    "entry": "src/app.py", "callable": ["wsgi_app", "application", "app"],
    "boot": {"ok_route": "/health", "html_route": "/ui", "json_roundtrip": "/notes"},
    "routes": [["POST", "/notes"], ["GET", "/notes"], ["GET", "/ui"]],
}
_STUB_MARKERS = ("NotImplementedError", "raise NotImplemented", "TODO", "FIXME",
                 "pass  # stub", "...  # todo")


def _synth(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name, body in _LEAVES.items():
        (src / name).write_text(body)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    return eng._synthesize_entry_code(_CONTRACT, mapping, unresolved), mapping


def test_synthesized_router_has_no_stub_markers(tmp_path):
    code, _ = _synth(tmp_path)
    assert code, "the engine must synthesize a router deterministically"
    for m in _STUB_MARKERS:
        assert m not in code, f"synthesized router must ship no stub ({m!r})"


def test_synthesized_router_branches_on_error_classes(tmp_path):
    code, _ = _synth(tmp_path)
    # a real dispatcher distinguishes unknown path (404) from wrong method (405)
    # and rejects a malformed body (400) — not a single catch-all.
    assert "404" in code, "router must branch on unknown path -> 404"
    assert "405" in code, "router must branch on wrong method -> 405"
    assert "400" in code, "router must reject a malformed/empty body -> 400"


def test_every_declared_route_resolves(tmp_path):
    _, mapping = _synth(tmp_path)
    for m, p in [("POST", "/notes"), ("GET", "/notes"), ("GET", "/ui")]:
        assert (m, p) in mapping, f"declared route {m} {p} left unrouted"


# ── READY is a logical AND (no green base contract over a red project) ────────

class _WS:
    enabled = True
    root = "/tmp/audit-ws"

    def __init__(self):
        self.written = {}

    def _write(self, name, body, *a, **k):
        self.written[name] = body


def _ready_engine(boots):
    eng = sfr.Engine.__new__(sfr.Engine)
    eng.workspace = _WS()
    eng.depth = sfr.DEPTH_PRODUCT
    eng._product_status = None
    eng._product_failed = []
    eng._assembled_product_boots = lambda *a, **k: boots
    eng._assembled_suite_failures = lambda *a, **k: []
    eng.emit = lambda *a, **k: None
    eng._constitution = []
    eng._goal = ""
    eng._standing_requirements = None
    return eng


_ACCEPT = {"smoke": [{"route": "/health", "expect": 200}],
           "e2e": [{"desc": "post/get", "route": "/notes"}]}


def test_ready_requires_a_green_integrate(tmp_path):
    eng = _ready_engine((True, "all routes answered"))
    eng._product_check(_ACCEPT, root_red=True)   # base boots, project is RED
    assert eng._product_status == "NOT READY", (
        "a passing base contract must not lift a RED project to READY")
    # (the green-integrate READY case is covered by
    # tests/coverage/test_product_check_boot_oracle::test_root_green_keeps_ready)


# ── the card-completeness gate actually reds an under-specified leaf ──────────

def _card_engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._constitution = ["POST /notes takes {text} -> {id}. src/app.py exposes"
                         " wsgi_app."]
    eng._goal = "notes"
    return eng


def test_card_gate_reds_exposing_leaf_without_acceptance(tmp_path):
    eng = _card_engine(tmp_path)
    node = {"id": "notes_post", "title": "handle POST /notes",
            "requirement": "create a note via POST /notes"}
    assert eng._card_completeness_findings(node), (
        "an exposing leaf with no acceptance must be flagged by the gate")
