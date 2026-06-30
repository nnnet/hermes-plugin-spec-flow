"""Phase 6 (deterministic, report-only) — the decomposition plan check surfaces
ownership problems as findings: a declared route with no owner leaf (orphan) or
two owner leaves (duplicate, e.g. the v122 second get_notes). DIAGNOSTIC only;
findings are plain JSON-safe strings and never flip READY.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    root = pathlib.Path(ws.root)
    (root / "specs").mkdir(parents=True, exist_ok=True)
    eng._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    return eng, root


def test_orphan_route_reported(tmp_path):
    eng, root = _engine(tmp_path)
    # a leaf owns /notes; NOTHING owns /health -> orphan
    (root / "specs" / "notes.md").write_text(
        "This leaf serves POST /notes and GET /notes.", encoding="utf-8")
    rep = eng._plan_ownership_report()
    assert any("/health" in f and "orphan" in f for f in rep)
    assert not any("/notes" in f and "orphan" in f for f in rep)


def test_duplicate_route_reported(tmp_path):
    eng, root = _engine(tmp_path)
    (root / "specs" / "health.md").write_text("serves GET /health", encoding="utf-8")
    (root / "specs" / "reader.md").write_text("GET /notes lists notes", encoding="utf-8")
    (root / "specs" / "writer.md").write_text(
        "POST /notes stores; also GET /notes", encoding="utf-8")
    rep = eng._plan_ownership_report()
    assert any("/notes" in f and "duplicate" in f for f in rep)


def test_report_is_json_safe(tmp_path):
    eng, root = _engine(tmp_path)
    (root / "specs" / "notes.md").write_text("GET /notes", encoding="utf-8")
    rep = eng._plan_ownership_report()
    json.dumps(rep)                      # must not raise
    assert all(isinstance(x, str) for x in rep)
