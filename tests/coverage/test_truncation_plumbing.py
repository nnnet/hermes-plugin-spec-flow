"""462: silent_truncation data source — a real producer (_inline_file cutting an
oversized embed) records the cut against the current node; the doctor drains it
as evidence['dropped'] and the truncation_marks detector fires. End-to-end of
the plumbing, offline, no LLM."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from harness import truncation_log as tl       # noqa: E402
import spec_flow_diagnosers as diag             # noqa: E402


def setup_function(_):
    tl.reset()


def test_record_is_scoped_to_node():
    tl.record("orphan:99")              # no scope -> dropped on the floor
    assert tl.drain("n1") == []
    with tl.node_scope("n1"):
        tl.record("src/big.py:5000")
    assert tl.drain("n1") == ["src/big.py:5000"]
    assert tl.drain("n1") == []         # drain is one-shot


def test_inline_file_records_real_drop(tmp_path):
    from harness import role_worker as rw
    big = tmp_path / "big.py"
    big.write_text("x" * (rw.INLINE_FILE_LIMIT + 250), encoding="utf-8")
    with tl.node_scope("leafA"):
        out = rw._inline_file(str(tmp_path), "big.py")
    assert "truncated" in out
    drops = tl.drain("leafA")
    assert drops and drops[0].startswith("big.py:")


def test_detector_fires_on_drained_evidence():
    d = diag.Diagnosers()
    findings = d.run(node="leafA", gate="spec_review", verdict="REJECT",
                     evidence={"dropped": ["big.py:250"]}, context=None)
    causes = [f.cause for f in findings]
    assert "silent_truncation" in causes


def test_detector_quiet_without_drops():
    d = diag.Diagnosers()
    findings = d.run(node="leafA", gate="spec_review", verdict="REJECT",
                     evidence={}, context=None)
    assert "silent_truncation" not in [f.cause for f in findings]
