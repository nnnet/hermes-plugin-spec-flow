"""Audit rule (revision P7, v150): the report HEAD must reflect the run's REAL
terminal outcome.

v150: ``render_report`` printed an unconditional «✅ (L0 integrate done)» into
the head of EVERY report — including a run whose root integrate recorded FAIL
and whose product verdict was NOT READY. The harness summary then classified
the run green by that substring. The head is a verdict carrier, not decor:
a red root or a NOT READY product must render as ❌.

Pure/deterministic: one cheap RunResult, no LLM, no run.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _result(loops=(), product_status=None, tasks=None):
    return sfr.RunResult(
        project={"name": "p", "goal": "g", "target": "t",
                 "tree": {"id": "L0", "title": "root"}},
        events=[], tasks=dict(tasks or {}), skills_used=set(),
        profiles_used=set(), loops=list(loops), gate_calls={},
        product_status=product_status)


def _head_line(report: str) -> str:
    # only the completion fragment: the same head line also carries the
    # skills/profiles checkmarks, which are unrelated to the run verdict
    line = next(ln for ln in report.splitlines() if "проект завершён" in ln)
    return line.split("проект завершён", 1)[1]


def test_head_is_red_on_root_integrate_fail():
    res = _result(loops=[{"type": "integrate-fail", "task": "L0",
                          "detail": "assembled suite RED"}])
    line = _head_line(sfr.render_report(res))
    assert "❌" in line, f"root FAIL must render red, got: {line!r}"
    assert "✅ (L0 integrate done)" not in line, (
        "the v150 unconditional green checkmark is back: " + repr(line))


def test_head_is_red_on_not_ready_product():
    res = _result(product_status="NOT READY")
    line = _head_line(sfr.render_report(res))
    assert "❌" in line and "NOT READY" in line, (
        f"NOT READY product must render red with the reason, got: {line!r}")


def test_head_is_red_on_failed_root_integrate_task():
    tasks = {"L0:integrate": sfr.Task(id="L0:integrate", title="Integrate",
                                      kind="integrate", profile="verifier",
                                      skill="spec-integrate",
                                      status="failed")}
    res = _result(tasks=tasks)
    line = _head_line(sfr.render_report(res))
    assert "❌" in line, f"failed root integrate task must render red: {line!r}"


def test_head_is_green_on_clean_run():
    res = _result(product_status="READY")
    line = _head_line(sfr.render_report(res))
    assert "✅" in line and "❌" not in line, (
        f"a clean READY run must render green, got: {line!r}")
