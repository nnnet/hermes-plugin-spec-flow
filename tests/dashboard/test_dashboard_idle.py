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
    # реализация is keyed authoritatively by role/event: architect + coder +
    # creator_candidate = 3 implement-stage calls. The 2 role-less, non-orchestra
    # calls (future_orchestra_step, bare call_start) stay un-attributable and are
    # counted via the gap window instead — never double-counted.
    impl = _cause_row(a, "реализация (LLM)")
    assert impl is not None and impl["llm"] == 3
    assert a["top"][0]["llm"] == 2          # the 2 ambiguous calls in the gap
    # multi-parameter usage still sees ALL 5 model-bearing calls (5 requests)
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


def _cause_row(a, cause):
    return next((r for r in a["by_cause"] if r["cause"] == cause), None)


def test_implement_llm_requests_survive_burst_collapsed_trace(tmp_path):
    # REGRESSION: the implement stage writes its milestones in a BURST after the
    # orchestra returns, so its trace rows share ~one timestamp; their gaps are
    # <=0 and get dropped. Counting "calls inside the surviving gap" then read 0
    # requests for реализация though the orchestra made many real calls. The fix
    # attributes LLM requests by node/role from the llm-log, not by trace gaps.
    epoch0 = 1_781_000_000.0
    rows = [
        _ev(epoch0, task="L0", phase="decompose"),
        # the engine spends 600s implementing, then bursts 3 milestones at once
        _ev(epoch0 + 600, tick=10, task="db:impl", phase="implement"),
        _ev(epoch0 + 600, tick=11, task="db:impl", phase="implement"),
        _ev(epoch0 + 600, tick=12, task="db:impl", phase="implement"),
    ]
    # 4 real implement-stage model calls logged run-relative, all BEFORE the
    # burst timestamp (their wall-window does not overlap any surviving gap).
    llm = [
        {"event": "orchestra_step", "t": 100.0, "node": "db",
         "role": "architect", "model": "m", "latency_s": 120.0, "wall": epoch0 + 100},
        {"event": "orchestra_step", "t": 230.0, "node": "db",
         "role": "coder", "model": "m", "latency_s": 130.0, "wall": epoch0 + 230},
        {"event": "orchestra_step", "t": 370.0, "node": "db",
         "role": "tester", "model": "m", "latency_s": 90.0, "wall": epoch0 + 370},
        {"event": "creator_candidate", "t": 470.0, "node": "db",
         "model": "m", "latency_s": 110.0, "wall": epoch0 + 470},
    ]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    impl = _cause_row(a, "реализация (LLM)")
    assert impl is not None
    # all 4 implement-stage calls counted from the llm-log, NOT 0
    assert impl["llm"] == 4
    # one logical op per node (db) — not an artifact of surviving gaps
    assert impl["count"] == 1
    # time = summed real call durations (120+130+90+110 = 450s), since the
    # harness logged latency_s — not the trace-gap wall time
    assert impl["total"] == 450.0


def test_implement_time_falls_back_to_trace_gap_without_latency(tmp_path):
    # an OLD run with no latency_s/wall on its call records: the request COUNT is
    # still authoritative from the llm-log, but the displayed TIME falls back to
    # the trace-gap wall time (we don't know per-call durations).
    epoch0 = 1_781_000_000.0
    rows = [_ev(epoch0, task="L0", phase="decompose"),
            _ev(epoch0 + 300, task="impl", phase="implement")]
    llm = [{"event": "orchestra_step", "t": 50.0, "node": "n", "role": "coder",
            "model": "m"},
           {"event": "orchestra_step", "t": 90.0, "node": "n", "role": "tester",
            "model": "m"}]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    impl = _cause_row(a, "реализация (LLM)")
    assert impl["llm"] == 2            # real count from llm-log
    assert impl["total"] == 300.0     # trace-gap wall time kept (no latency)


def test_integration_keeps_pytest_time_only_count_from_llm(tmp_path):
    # интеграция = mostly pytest; even when the verifier makes an LLM judgment
    # call with a real latency, the cause TIME must stay trace-gap-sourced (the
    # pytest wall time), only the request COUNT is taken from the llm-log.
    epoch0 = 1_781_000_000.0
    rows = [_ev(epoch0, task="x", phase="review"),
            _ev(epoch0 + 200, task="x", phase="integrate",
                action="end-to-end acceptance")]
    llm = [{"event": "call_start", "t": 10.0, "node": "x", "role": "verifier",
            "model": "m", "latency_s": 5.0, "wall": epoch0 + 10}]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    integ = _cause_row(a, "интеграция (тесты)")
    assert integ["llm"] == 1          # verifier call counted
    assert integ["total"] == 200.0    # full pytest gap, NOT the 5s LLM latency


def test_latency_is_spliced_from_call_ok_onto_call_start(tmp_path):
    # timed_ask logs the real duration on call_ok (which has NO model, so it is
    # not a request). The analysis must splice that latency onto the matching
    # call_start request so реализация time reflects real model spend.
    epoch0 = 1_781_000_000.0
    rows = [_ev(epoch0, task="L0", phase="decompose"),
            _ev(epoch0 + 999, task="impl", phase="implement")]
    llm = [
        {"event": "call_start", "t": 10.0, "node": "n", "role": "implementer",
         "model": "m"},
        {"event": "call_ok", "t": 70.0, "node": "n", "role": "implementer",
         "latency_s": 60.0, "wall": epoch0 + 70},
        {"event": "call_start", "t": 100.0, "node": "n", "role": "implementer",
         "model": "m"},
        {"event": "call_ok", "t": 140.0, "node": "n", "role": "implementer",
         "latency_s": 40.0, "wall": epoch0 + 140},
    ]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    impl = _cause_row(a, "реализация (LLM)")
    assert impl["llm"] == 2            # two implementer requests
    # time = spliced latencies (60+40), NOT the 999s trace gap
    assert impl["total"] == 100.0


def test_call_cause_keys_by_role_not_window(tmp_path):
    # a decomposer call logged with a timestamp that lands inside an implement
    # gap must still be attributed to декомпозиция by its ROLE, not to the gap's
    # реализация cause. This is the node/role-keyed attribution the fix adds.
    epoch0 = 1_781_000_000.0
    rows = [_ev(epoch0, task="L0", phase="implement"),
            _ev(epoch0 + 300, task="impl", phase="implement")]
    llm = [{"event": "call_start", "t": 150.0, "node": "L0",
            "role": "decomposer", "model": "m"}]
    a = dash._idle_analysis(_run(tmp_path, rows, llm))
    dec = _cause_row(a, "декомпозиция (LLM)")
    impl = _cause_row(a, "реализация (LLM)")
    assert dec is not None and dec["llm"] == 1   # keyed by role -> decompose
    # the implement gap's own per-gap LLM count is not inflated by the
    # decomposer call (реализация has no llm-log calls -> stays 0)
    assert (impl is None) or (impl["llm"] == 0)


# ─── failure report: abnormal responses → fallback / error (Part 2) ──────

def test_failures_report_aggregates_abnormal_and_fallbacks(tmp_path):
    trace = [_ev(0, task="L0", phase="review"), _ev(5, task="L0", phase="review")]
    llm = [
        {"event": "llm_attempt", "model": "m_a", "provider": "openrouter",
         "status": 429, "abnormal": True, "slept_s": 3.0, "t": 1.0},
        {"event": "llm_attempt", "model": "m_a", "provider": "openrouter",
         "status": 429, "abnormal": True, "slept_s": 2.0, "t": 2.0},
        {"event": "llm_attempt", "model": "m_a", "provider": "openrouter",
         "status": 200, "abnormal": False, "t": 3.0},
        {"event": "llm_fallback", "from_model": "m_a", "to_model": "m_b",
         "reason": "429", "terminal": False, "t": 3.1},
        {"event": "llm_fallback", "from_model": "m_b", "to_model": None,
         "reason": "429", "terminal": True, "t": 4.0},
    ]
    f = dash._idle_analysis(_run(tmp_path, trace, llm))["failures"]
    a = next(r for r in f["by_model"] if r["model"] == "m_a")
    assert a["abn"] == 2 and a["429"] == 2 and a["delay_s"] == 5.0
    assert a["fallbacks"] == 1
    b = next(r for r in f["by_model"] if r["model"] == "m_b")
    assert b["fallbacks"] == 1 and b["errors"] == 1     # terminal counts as error
    assert f["totals"]["abnormal"] == 2
    assert f["totals"]["terminal"] == 1
    assert f["totals"]["delay_s"] == 5.0
    assert any(t["terminal"] and t["to"] is None for t in f["transitions"])


def test_failures_report_empty_on_clean_run(tmp_path):
    trace = [_ev(0, task="L0", phase="review"), _ev(2, task="L0", phase="implement")]
    llm = [{"event": "llm_attempt", "model": "m_a", "status": 200,
            "abnormal": False, "t": 1.0}]
    f = dash._idle_analysis(_run(tmp_path, trace, llm))["failures"]
    assert f["by_model"] == [] and f["totals"]["abnormal"] == 0
    assert f["totals"]["fallbacks"] == 0


def test_abnormal_attempt_not_double_counted_as_request(tmp_path):
    # llm_attempt / llm_fallback must NOT inflate the LLM request count
    trace = [_ev(0, task="L0", phase="review"), _ev(5, task="L0", phase="review")]
    llm = [
        {"event": "call_start", "role": "reviewer", "model": "m_a", "t": 0.5},
        {"event": "llm_attempt", "model": "m_a", "status": 429,
         "abnormal": True, "slept_s": 1.0, "t": 1.0},
        {"event": "call_ok", "role": "reviewer", "node": "L0",
         "latency_s": 2.0, "t": 3.0},
    ]
    a = dash._idle_analysis(_run(tmp_path, trace, llm))
    total_req = sum(c.get("req", 0) for c in a["by_cause"])
    assert total_req <= 1            # the single call_start, not the attempt too
