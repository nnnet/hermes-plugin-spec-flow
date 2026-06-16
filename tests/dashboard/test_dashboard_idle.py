"""Duration / idle analysis for the ⏱ dashboard tab: _idle_analysis turns a
trace into per-operation gaps with an attributed cause + roll-ups, so a run's
time sinks (spec review, integration, quota waits) are visible and sortable."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _run(tmp_path, trace_rows, llm_rows=None):
    d = tmp_path / "run"
    d.mkdir()
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(r) for r in trace_rows), encoding="utf-8")
    if llm_rows is not None:
        (d / "llm-log.jsonl").write_text(
            "\n".join(json.dumps(r) for r in llm_rows), encoding="utf-8")
    return d


def _ev(t, **kw):
    base = {"tick": int(t), "phase": "", "task": "", "action": "", "t": float(t)}
    base.update(kw)
    return base


def test_none_run():
    assert dash._idle_analysis(None) == {"empty": True}


def test_gap_is_attributed_to_next_event(tmp_path):
    rows = [_ev(0, task="a", phase="review"),
            _ev(10, task="b", phase="implement"),
            _ev(13, task="c", phase="implement")]
    a = dash._idle_analysis(_run(tmp_path, rows))
    # a gap is the time spent PRODUCING the next event, so it is attributed to
    # that next event: 10s -> b, 3s -> c.
    durs = {r["node"]: r["dur"] for r in a["top"]}
    assert durs["b"] == 10.0 and durs["c"] == 3.0
    assert a["total_s"] == 13.0 and a["events"] == 2


def test_cause_classification(tmp_path):
    rows = [_ev(0, task="x", phase="review"),
            _ev(5, task="x", phase="integrate", action="end-to-end acceptance"),
            _ev(40, task="x", phase="implement", action="rework round 1"),
            _ev(60, task="x", phase="implement")]
    a = dash._idle_analysis(_run(tmp_path, rows))
    # next-event attribution: gap before tick T is labelled by event T's work
    causes = {r["tick"]: r["cause"] for r in a["top"]}
    assert causes[5] == "интеграция (тесты)"
    assert causes[40] == "починка"
    assert causes[60] == "реализация (LLM)"


def test_llm_calls_counted_when_log_is_run_relative(tmp_path):
    # the trace stamps EPOCH seconds; the llm-log stamps RUN-RELATIVE seconds.
    # comparing the two bases directly made every per-gap LLM count read 0
    # (decompose/review/implement showed 0 requests though they ARE LLM calls).
    epoch0 = 1_781_000_000.0
    rows = [_ev(epoch0, task="d", phase="decompose"),
            _ev(epoch0 + 300, task="d", phase="decompose")]
    # two calls fired inside the gap, logged run-relative (100s, 200s). A real
    # LLM call always names its `model` — that is the universal request marker.
    llm = [{"event": "call_start", "t": 100.0, "model": "m"},
           {"event": "call_start", "t": 200.0, "model": "m"}]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    assert a["top"][0]["llm"] == 2
    assert sum(r.get("llm", 0) for r in a["by_cause"]) == 2


def test_llm_calls_counted_universally_by_model_field(tmp_path):
    # an LLM call is ANY event carrying a `model` field, whatever the worker
    # names it (call_start, orchestra_step, creator_candidate, a future
    # orchestra step). Result records (outcome) and bookkeeping events (no
    # model) are excluded. Role-independent and future-proof.
    rows = [_ev(0, task="impl", phase="implement"),
            _ev(300, task="impl", phase="implement")]
    llm = [{"event": "orchestra_step", "t": 50.0, "role": "architect", "model": "m"},
           {"event": "orchestra_step", "t": 90.0, "role": "coder", "model": "m"},
           {"event": "creator_candidate", "t": 150.0, "model": "m"},
           {"event": "future_orchestra_step", "t": 180.0, "model": "m"},
           {"event": "call_start", "t": 200.0, "model": "m"},
           {"event": "outcome", "t": 210.0, "model": "m"},     # result — excluded
           {"event": "commit_queue", "t": 220.0}]              # no model — ignored
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    assert a["top"][0]["llm"] == 5
    # multi-parameter usage is discovered from whatever was logged
    u = a["llm_usage"]
    assert u["total"] == 5
    assert "role" in u["dims"]
    by_role = {r["key"]: r["count"] for r in u["by_role"]}
    assert by_role.get("architect") == 1 and by_role.get("coder") == 1


def test_quota_wait_window_is_detected(tmp_path):
    rows = [_ev(0, task="x", phase="implement"),
            _ev(300, task="x", phase="implement")]
    # a quota_wait logged inside the [0,300] gap → cause = ожидание квоты
    llm = [{"event": "quota_wait", "t": 100.0, "wait_s": 200}]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    assert a["top"][0]["cause"] == "ожидание квоты"


def test_rollups_sorted_by_total(tmp_path):
    rows = [_ev(0, task="a", phase="review"),
            _ev(100, task="b", phase="integrate", action="acceptance"),
            _ev(110, task="b", phase="integrate", action="acceptance"),
            _ev(130, task="a", phase="review")]
    a = dash._idle_analysis(_run(tmp_path, rows))
    # next-event attribution: 100s+10s -> integrate, 20s -> the final review
    by_cause = {r["cause"]: r["total"] for r in a["by_cause"]}
    assert by_cause["интеграция (тесты)"] == 110.0
    assert by_cause["ревью спеки"] == 20.0
    # by_cause is sorted descending
    totals = [r["total"] for r in a["by_cause"]]
    assert totals == sorted(totals, reverse=True)


def test_zero_and_negative_gaps_skipped(tmp_path):
    rows = [_ev(0, task="a"), _ev(0, task="b"), _ev(5, task="c")]
    a = dash._idle_analysis(_run(tmp_path, rows))
    # the 0-gap pair is dropped; only the 5s gap counts
    assert a["events"] == 1 and a["total_s"] == 5.0
