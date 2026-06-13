"""compare_runs metric extraction: a synthetic run dir in, the right
numbers out. The tool is how 'memory on/off', 'parallel on/off',
'pipeline on/off' get measured — its arithmetic must be trustworthy."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import compare_runs as cr  # noqa: E402


def _write(run, name, rows):
    (run / name).write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _make_run(tmp_path):
    run = tmp_path / "2026-06-13T00-00-00__v099__p4-b2b-marketplace"
    run.mkdir()
    _write(run, "trace.jsonl", [
        {"tick": 1, "t": 1000.0, "action": "start"},
        {"tick": 2, "t": 1005.0, "action": "rework after review REJECT"},
        {"tick": 3, "t": 1010.0, "action": "childless branch demoted to leaf"},
        {"tick": 4, "t": 1030.0, "gate": "integrate_verify",
         "verdict": "PASS"},
        {"tick": 5, "t": 1040.0, "gate": "requirement", "verdict": "ATTACHED"},
        {"tick": 6, "t": 1100.0, "task": "L0", "action": "L0 integrate RED"},
    ])
    _write(run, "llm-log.jsonl", [
        {"event": "call_start"}, {"event": "call_start"},
        {"event": "quota_wait"}, {"event": "auto_answer"},
        {"event": "integrate_red"},
    ])
    (run / "tree.json").write_text(json.dumps(
        {"id": "L0", "children": [{"id": "a"}, {"id": "b", "children":
                                                [{"id": "c"}]}]}),
        encoding="utf-8")
    (run / "meta.json").write_text(json.dumps(
        {"case": "p4-b2b-marketplace", "worker_models": {"x": "y"}}),
        encoding="utf-8")
    return run


def test_metrics_extracted_correctly(tmp_path):
    m = cr.metrics(_make_run(tmp_path))
    assert m["run"] == "v099"
    assert m["duration_s"] == 100.0          # 1100 - 1000
    assert not m["finished"]                  # no SUMMARY.md
    assert m["duration"].endswith("~")        # ongoing marker
    assert m["ticks"] == 6
    assert m["tree_nodes"] == 4               # L0 + a + b + c
    assert m["llm_calls"] == 2
    assert m["reviews_rejected"] == 1
    assert m["demotions"] == 1
    assert m["integrate_pass"] == 1
    assert m["integrate_red"] == 1
    assert m["quota_waits"] == 1
    assert m["auto_answers"] == 1
    assert m["reqs_attached"] == 1
    assert m["root_red"] is True


def test_finished_run_has_no_tilde(tmp_path):
    run = _make_run(tmp_path)
    (run / "SUMMARY.md").write_text("done", encoding="utf-8")
    m = cr.metrics(run)
    assert m["finished"]
    assert not m["duration"].endswith("~")


def test_render_table_has_a_row_per_run(tmp_path):
    run = _make_run(tmp_path)
    table = cr.render_table([cr.metrics(run)])
    assert "v099" in table
    lines = table.splitlines()
    assert len(lines) == 3      # header + rule + one data row


def test_empty_trace_does_not_crash(tmp_path):
    run = tmp_path / "2026-06-13T00-00-00__v100__p4-b2b-marketplace"
    run.mkdir()
    (run / "trace.jsonl").write_text("", encoding="utf-8")
    m = cr.metrics(run)
    assert m["duration_s"] == 0.0 and m["ticks"] == 0
