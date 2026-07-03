"""Audit rule S10.26: the late-req delta gate excludes routes ALREADY OWNED
by another leaf using the ownership DATUM (`_route_owners`), not only sibling
dispatch prose — a node is held to the routes it ADDS, never to a dependency
it narrates.

v156 (p6-micro-notes), trace events 79-80: the web_ui late requirement's spec
quoted the acceptance wording 'GET /health', core's canonical handler
(get_health) carries NO literal '/health' string, so the sibling-served scan
(`_served_routes` — dispatch evidence only) missed the owner and the delta
gate FAILed web_ui with "no handler for ['/health']". The doctor opened
web_ui:empty_delta (remedy reject_empty) and that cause stayed OPEN for the
whole run, polluting the final root report ('doctor causes still open:
web_ui:empty_delta'). The datum knew better all along: ('GET', '/health') was
recorded to core in `_route_owners` (S10.19 first-owner-wins).

Contract pinned here:
  * a verb-declared route in the node's spec that the ownership datum records
    to ANOTHER leaf is a dependency — the delta gate must not demand its
    handler from this node;
  * the node's OWN routes (owned by nobody else, or by itself) are still
    demanded — the v062 class (about_page declared GET /about, added nothing)
    stays red.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [("GET", "/ui")],
}

# core's canonical handler carries NO literal '/health' — dispatch-evidence
# scans cannot see the owner (the exact v156 shape)
_CORE = '''\
def get_health(payload, query):
    return 200, {"status": "ok"}


def post_notes(payload, query):
    return 201, {"id": 1}
'''

_WEB_UI = '''\
def get_ui(payload, query):
    return 200, "<html><body>notes</body></html>"
'''


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    root = pathlib.Path(eng.workspace.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "specs").mkdir(parents=True, exist_ok=True)
    (root / "src" / "core.py").write_text(_CORE, encoding="utf-8")
    # core registers its routes in the ownership datum, as a live run does
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes, GET /notes and GET /health"}
    assert ("GET", "/health") in set(eng._leaf_owned_routes(core))
    return eng


def _web_ui_gate(eng, spec_md: str) -> "tuple[bool, list]":
    root = pathlib.Path(eng.workspace.root)
    (root / "src" / "web_ui.py").write_text(_WEB_UI, encoding="utf-8")
    (root / "specs" / "web_ui.md").write_text(
        textwrap.dedent(spec_md), encoding="utf-8")
    node = {"id": "web_ui", "title": "minimal web interface",
            "_late_req": True}
    ok = eng._late_req_delta_gate(node, "web_ui", 1, "src/web_ui.py")
    return ok, [l for l in eng.loops if l["type"] == "empty-delta"]


def test_foreign_owned_route_in_spec_is_a_dependency_not_a_delta(tmp_path):
    eng = _engine(tmp_path)
    ok, loops = _web_ui_gate(eng, """\
        # Minimal web interface

        Serve GET /ui as a server-rendered HTML page.
        Acceptance context: the service already answers GET /health with 200.
    """)
    assert ok and not loops, (
        "the datum records GET /health to core — charging web_ui with it is "
        "the v156 misfire (open doctor cause web_ui:empty_delta): %s" % loops)


def test_own_new_route_is_still_demanded(tmp_path):
    # the v062 class stays red: a route NOBODY owns, declared by this node,
    # with no handler delivered
    eng = _engine(tmp_path)
    ok, loops = _web_ui_gate(eng, """\
        # Minimal web interface

        Serve GET /ui and also GET /about as HTML pages.
    """)
    assert not ok and loops, (
        "GET /about is this node's OWN new route with no handler — the gate "
        "must stay red")
    assert "/about" in loops[0]["detail"]
