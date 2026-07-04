"""Audit rule S12.16 (v164): the module-rework directive carries the
CONTRACTED MEDIA of every route it re-prints — the S11.4/S12.2
frozen-surface reprint pattern extended to the body medium.

v164 (2026-07-03T23-02-13__v164__p6-micro-notes): the integrate doctor's
"rework core (acceptance blamed it)" directive re-printed the module's
route surface (`_module_route_binding_text`, the S12.2 reprint) — route,
canonical handler, owner leaf — but NOT the contracted body medium. The
rewriting model swapped get_about's HTML page for a JSON dict
({"name": "notes-service", "version": "1.0"}); contracts/interface.json
contracted GET /about as text/html the whole time. The datum existed
(`_route_media_map`), the directive just never carried it, so the model
re-guessed the medium exactly like v159 re-guessed the route surface.

Contract enforced (deterministic):
  * `_module_route_binding_text` — the block printed into the module-rework
    directive — carries the contracted media line for every owned route
    that HAS a media datum (text/html routes additionally spell out
    "an HTML page string, never a JSON dict");
  * GREEN edge: a route with NO media datum gets NO fabricated media line
    (lenient — the engine never invents a medium).

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [("GET", "/about"), ("GET", "/legacy")],
    "media": {"/notes": "json", "/health": "json", "/about": "html"},
}
_CORE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}
_ABOUT = {"id": "about_page", "title": "About page",
          "code_target": "src/core.py", "binds_route": ["GET", "/about"],
          "requirement": "serve GET /about as a small HTML page"}
_LEGACY = {"id": "legacy_page", "title": "Legacy page",
           "code_target": "src/core.py", "binds_route": ["GET", "/legacy"],
           "requirement": "serve GET /legacy"}       # no media datum


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    # record ownership the way the run does: core's own routes, then the
    # amends' engine-bound routes into the SAME module (code_target)
    eng._leaf_owned_routes(dict(_CORE))
    eng._leaf_owned_routes(dict(_ABOUT))
    eng._leaf_owned_routes(dict(_LEGACY))
    return eng


# ---- RED: the v164 media re-guess -------------------------------------------

def test_rework_directive_carries_contracted_media(tmp_path):
    eng = _engine(tmp_path)
    block = eng._module_route_binding_text("core")
    assert "get_about" in block and "/about" in block  # S12.2 baseline holds
    about_line = next(ln for ln in block.splitlines() if "/about" in ln)
    assert "text/html" in about_line, (
        "v164: the rework directive re-printed GET /about's handler and "
        "owner but NOT its contracted media — the rewriting model swapped "
        "the HTML page for a JSON dict while interface.json said text/html; "
        "the media datum must ride the same frozen-surface reprint: %r"
        % about_line)


def test_json_routes_carry_their_media_too(tmp_path):
    eng = _engine(tmp_path)
    block = eng._module_route_binding_text("core")
    notes_lines = [ln for ln in block.splitlines() if "`GET /notes`" in ln]
    assert notes_lines and "application/json" in notes_lines[0], (
        "a contracted JSON route carries its media line the same way: %r"
        % notes_lines)


# ---- GREEN: no datum = no fabricated media line -------------------------------

def test_route_without_media_datum_gets_no_media_line(tmp_path):
    eng = _engine(tmp_path)
    block = eng._module_route_binding_text("core")
    legacy_line = next(ln for ln in block.splitlines() if "/legacy" in ln)
    assert "media" not in legacy_line.lower(), (
        "the engine never INVENTS a medium — a route with no media datum "
        "must get no media line: %r" % legacy_line)
