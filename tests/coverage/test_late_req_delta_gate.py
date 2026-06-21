"""437: delta as acceptance. A late-requirement leaf that produced no real
delta (its owned module is missing or only blanks/comments) must NOT pass as
implemented — the gate records an empty-delta loop and routes the empty_delta
cause to the doctor. Decided by the artifact, not the model. Inert for ordinary
nodes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine, Workspace   # noqa: E402


def _engine(tmp):
    e = Engine.__new__(Engine)
    e.workspace = Workspace(root=tmp, enabled=True).open()
    e.review_policy = {"min_delta_lines": 1}
    e.loops = []
    e._doctor_calls = []
    e._doctor_advise = lambda *a, **k: e._doctor_calls.append((a, k)) or None
    e.emit = lambda *a, **k: None
    return e


def _write(tmp, rel, body):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(body)


def test_non_late_node_is_inert():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        assert e._late_req_delta_gate({"id": "n"}, "n", 4, "src/x.py") is True
        assert e.loops == [] and e._doctor_calls == []


def test_missing_owner_file_is_empty_delta():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/gone.py")
        assert ok is False
        assert any(lp["type"] == "empty-delta" for lp in e.loops)
        assert e._doctor_calls and e._doctor_calls[0][0][3] == "delta_gate"


def test_blank_and_comment_only_is_empty_delta():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/hollow.py", "\n\n# just a comment\n   \n")
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/hollow.py")
        assert ok is False
        # the finding fed to the doctor names an empty delta the detector reads
        ev = e._doctor_calls[0][0][5]
        assert "adds no new symbol" in ev["scope_findings"][0]


def test_real_code_passes():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/real.py", "def handler(req):\n    return 200, {}\n")
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/real.py")
        assert ok is True
        assert e.loops == [] and e._doctor_calls == []
