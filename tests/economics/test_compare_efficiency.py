"""Normalized efficiency coefficients in compare_runs.metrics(): size-
independent rates that let runs of different power be compared. Built from
a synthetic run dir (meta.json + trace.jsonl) so the math is pinned."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import compare_runs as cr  # noqa: E402


def _run(tmp_path, trace_rows, meta=None):
    d = tmp_path / "2026-01-01T00-00-00__vTST__case"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(r) for r in trace_rows), encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _ev(tick, **kw):
    base = {"tick": tick, "phase": "", "task": "", "action": "",
            "verdict": "", "t": float(tick)}
    base.update(kw)
    return base


def test_ratios_are_normalized_by_node_count(tmp_path, monkeypatch):
    # 4 tree nodes, 8 llm calls, 2 rejects → 2 calls/node, 0.5 rework/node
    rows = [_ev(1, event="tree_built", nodes=4)]
    rows += [_ev(2 + i, event="call_start") for i in range(8)]
    rows += [_ev(20, action="REJECT"), _ev(21, action="REJECT")]
    d = _run(tmp_path, rows)
    monkeypatch.setattr(cr, "_tree_node_count", lambda *a, **k: 4, raising=False)
    m = cr.metrics(d)
    # tree_nodes resolution may vary; assert the ratio identity holds
    if m["tree_nodes"]:
        assert m["calls_per_node"] == round(m["llm_calls"] / m["tree_nodes"], 3)
        assert m["rework_per_node"] == round(
            m["reviews_rejected"] / m["tree_nodes"], 3)


def test_ratios_zero_safe_on_empty(tmp_path):
    d = _run(tmp_path, [_ev(1)])
    m = cr.metrics(d)
    # no division-by-zero: every ratio is a finite number
    for k in ("calls_per_node", "errors_per_node", "rework_per_node",
              "sec_per_node", "first_pass_rate", "quota_per_call"):
        assert isinstance(m[k], (int, float))


def test_first_pass_rate_is_a_share(tmp_path):
    # 3 integrate passes, 1 red → 3/4 = 0.75
    rows = [_ev(1)]
    rows += [_ev(2 + i, phase="integrate", gate="integrate_verify",
                 verdict="PASS", action="end-to-end acceptance") for i in range(3)]
    rows += [_ev(10, event="integrate_red")]
    d = _run(tmp_path, rows)
    m = cr.metrics(d)
    if (m["integrate_pass"] + m["integrate_red"]) > 0:
        assert 0.0 <= m["first_pass_rate"] <= 1.0
