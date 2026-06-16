"""Phase 5 dashboard: the team card + the orchestra specialist call sequence.

These cover the team-aware additions to the flow tab and the inputs tab:
  * ``_orchestra_sequences`` reconstructs the ordered specialist chain per node
    from the llm-log (call_start carries step+model, call_ok carries latency).
  * ``_flow_orchestra_html`` renders that chain (added beneath the existing
    milestone time-axis — the solo form is unchanged).
  * ``_team_card_md`` lists each specialist → role → provider → model → params,
    read from the persisted case config OR reconstructed from the log.
  * end-to-end ``_build_state`` shows a team card + specialist breakdown for an
    orchestra run, and neither for a solo run (a single implement milestone only).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402


def _run(tmp_path, *, trace=None, llm=None, meta=None, inputs=None):
    d = tmp_path / "run"
    d.mkdir()
    if trace is not None:
        (d / "trace.jsonl").write_text(
            "\n".join(json.dumps(r) for r in trace), encoding="utf-8")
    if llm is not None:
        (d / "llm-log.jsonl").write_text(
            "\n".join(json.dumps(r) for r in llm), encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if inputs is not None:
        (d / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
    return d


def _ev(t, **kw):
    base = {"tick": int(t), "phase": "", "task": "", "action": "", "t": float(t)}
    base.update(kw)
    return base


# a real orchestra: orchestra_start announces the roster, then each specialist's
# call is a call_start (step+model) paired with a call_ok (latency_s). A loop
# tester→fixer→tester means `tester` appears twice.
def _orchestra_llm(node="db", epoch0=1_781_000_000.0):
    seq = [("architect", "qwen3-coder", 12.0),
           ("coder", "qwen3-coder", 30.0),
           ("tester", "qwen3-coder", 18.0),
           ("fixer", "deepseek", 25.0),
           ("tester", "qwen3-coder", 9.0)]          # tester→fixer→tester loop
    rows = [{"event": "orchestra_start", "node": node, "t": 0.0,
             "wall": epoch0, "steps": ["architect", "coder", "tester", "fixer"]}]
    t = 1.0
    for step, model, lat in seq:
        rows.append({"event": "call_start", "node": node, "role": "implementer",
                     "step": step, "mode": "orchestra", "model": model,
                     "t": t, "wall": epoch0 + t})
        rows.append({"event": "call_ok", "node": node, "role": "implementer",
                     "mode": "orchestra", "latency_s": lat,
                     "t": t + lat, "wall": epoch0 + t + lat})
        t += lat + 1.0
    return rows


# ── _orchestra_sequences ─────────────────────────────────────────────────────
def test_sequence_is_ordered_with_model_and_duration():
    seqs = dash._orchestra_sequences(_orchestra_llm())
    assert set(seqs) == {"db"}
    steps = seqs["db"]
    assert [s["step"] for s in steps] == \
        ["architect", "coder", "tester", "fixer", "tester"]
    # the loop repeats `tester`
    assert [s["step"] for s in steps].count("tester") == 2
    # per-step model + real latency spliced from the paired call_ok
    assert steps[0]["step"] == "architect" and steps[0]["model"] == "qwen3-coder"
    assert steps[0]["latency_s"] == 12.0
    assert steps[3]["step"] == "fixer" and steps[3]["model"] == "deepseek"
    assert steps[3]["latency_s"] == 25.0
    assert steps[4]["latency_s"] == 9.0          # second tester pass


def test_sequence_seeds_announced_node_with_no_calls_yet():
    # orchestra_start announced the team before any specialist answered
    llm = [{"event": "orchestra_start", "node": "n", "steps": ["architect"]}]
    seqs = dash._orchestra_sequences(llm)
    assert seqs == {"n": []}


def test_solo_run_has_no_sequences():
    llm = [{"event": "call_start", "node": "x", "role": "implementer",
            "model": "m"},                       # no step / mode==orchestra
           {"event": "call_ok", "node": "x", "role": "implementer",
            "latency_s": 5.0}]
    assert dash._orchestra_sequences(llm) == {}


# ── _flow_orchestra_html ─────────────────────────────────────────────────────
def test_flow_orchestra_html_shows_each_specialist_model_duration():
    html = dash._flow_orchestra_html(_orchestra_llm())
    assert html is not None
    for role in ("architect", "coder", "tester", "fixer"):
        assert role in html
    assert "qwen3-coder" in html and "deepseek" in html
    assert "12.0s" in html and "25.0s" in html      # real durations rendered


def test_flow_orchestra_html_none_for_solo():
    assert dash._flow_orchestra_html(
        [{"event": "call_start", "node": "x", "model": "m"}]) is None
    assert dash._flow_orchestra_html([]) is None


# ── _flow_html composition (existing axis + orchestra breakdown) ─────────────
def test_flow_html_appends_orchestra_after_axis():
    trace = [_ev(1000, task="db:impl", phase="implement", action="leaf", level=1),
             _ev(1005, task="db:impl", phase="implement", action="green", level=1)]
    axis = dash._flow_timeaxis(trace)
    full = dash._flow_html(trace, None, _orchestra_llm())
    assert full is not None and full.startswith(axis)      # axis form unchanged
    assert "architect" in full and "🎻" in full            # breakdown appended


def test_flow_html_solo_is_axis_only():
    trace = [_ev(1000, task="x", phase="implement", level=1),
             _ev(1005, task="x", phase="implement", level=1)]
    solo_llm = [{"event": "call_start", "node": "x", "model": "m"}]
    assert dash._flow_html(trace, None, solo_llm) == dash._flow_timeaxis(trace)


# ── _team_card_md ────────────────────────────────────────────────────────────
def test_team_card_from_persisted_specialists_shape(tmp_path):
    # the richer shape: team.specialists with role + provider + model + params
    meta = {"workers": {"implementer": {"team": {"specialists": [
        {"role": "architect", "provider": "local",
         "model": "qwen3-coder", "params": {"temperature": 0.2}},
        {"role": "coder", "provider": "hermes", "model": "senior-dev"},
    ]}}}}
    d = _run(tmp_path, meta=meta, llm=[])
    md = "\n".join(dash._team_card_md(d, []))
    assert "Команда" in md
    assert "architect" in md and "coder" in md
    assert "local" in md and "hermes" in md
    assert "qwen3-coder" in md and "senior-dev" in md
    assert "temperature=0.2" in md


def test_team_card_from_bare_list_shape(tmp_path):
    # today's shape: team is a bare [{role}…] list; provider defaults to local,
    # model is filled from the llm-log when the config omits it
    meta = {"workers": {"implementer": {"team": [
        {"role": "architect"}, {"role": "coder"},
        {"role": "tester"}, {"role": "fixer"}]}}}
    d = _run(tmp_path, meta=meta, llm=_orchestra_llm())
    md = "\n".join(dash._team_card_md(d, _orchestra_llm()))
    assert "architect" in md and "fixer" in md
    assert "local" in md                       # provider default
    assert "qwen3-coder" in md                 # model recovered from the log


def test_team_card_reconstructed_from_log_without_config(tmp_path):
    # no persisted workers block at all — the roster + models come from the log
    d = _run(tmp_path, llm=_orchestra_llm())
    md = "\n".join(dash._team_card_md(d, _orchestra_llm()))
    assert "architect" in md and "coder" in md
    assert "deepseek" in md or "qwen3-coder" in md


def test_team_card_empty_for_solo(tmp_path):
    d = _run(tmp_path, llm=[{"event": "call_start", "node": "x", "model": "m"}])
    assert dash._team_card_md(d, []) == []


# ── end-to-end through _build_state ──────────────────────────────────────────
def test_build_state_orchestra_run_has_card_and_sequence(tmp_path):
    trace = [_ev(1000, task="db:impl", phase="implement", action="leaf", level=1),
             _ev(1010, task="db:impl", phase="implement", action="green", level=1)]
    meta = {"case": "p6", "workers": {"implementer": {"team": [
        {"role": "architect"}, {"role": "coder"},
        {"role": "tester"}, {"role": "fixer"}]}}}
    d = _run(tmp_path, trace=trace, llm=_orchestra_llm(), meta=meta,
             inputs={"goal": "notes"})
    st = dash._build_state(d)
    # team card on the inputs tab
    assert "Команда" in st["reports"]["inputs"]
    assert "architect" in st["reports"]["inputs"]
    # specialist sequence on the flow tab, beneath the milestone axis
    flow = st["reports"]["flow"]
    assert flow and "🎻" in flow
    assert "architect" in flow and "fixer" in flow


def test_build_state_solo_run_no_card_single_implement_step(tmp_path):
    trace = [_ev(1000, task="x", phase="implement", action="leaf code", level=1),
             _ev(1005, task="x", phase="implement", action="green", level=1)]
    solo_llm = [{"event": "call_start", "node": "x", "role": "implementer",
                 "model": "m"},
                {"event": "call_ok", "node": "x", "role": "implementer",
                 "latency_s": 4.0}]
    d = _run(tmp_path, trace=trace, llm=solo_llm, inputs={"goal": "g"})
    st = dash._build_state(d)
    # no team card
    assert "Команда" not in st["reports"]["inputs"]
    # flow tab unchanged: the milestone axis renders, with NO orchestra breakdown
    # appended (a solo node shows its single implement milestone as before)
    flow = st["reports"]["flow"]
    assert flow and "🎻" not in flow
    assert dash._flow_orchestra_html(solo_llm) is None
    assert "implement" in flow                       # the single milestone box
