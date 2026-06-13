"""#6: real token accounting. The OpenAI path logs token_usage (from the API
usage field) tagged by role+model; compare_runs sums it, and the dashboard
rolls it up by role+model. Real economics, not call counts."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb  # noqa: E402
import compare_runs as cr  # noqa: E402
import live_dashboard as dash  # noqa: E402


# ─── backend logging ──────────────────────────────────────────────────

def test_log_token_usage_emits_event(tmp_path, monkeypatch):
    log = tmp_path / "llm.jsonl"
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(log))
    lb._call_ctx.role = "implementer"
    lb._log_token_usage("claude/haiku",
                        {"prompt_tokens": 120, "completion_tokens": 45})
    rec = json.loads(log.read_text().strip().splitlines()[-1])
    assert rec["event"] == "token_usage"
    assert rec["role"] == "implementer" and rec["model"] == "claude/haiku"
    assert rec["prompt_tokens"] == 120 and rec["completion_tokens"] == 45


def test_log_token_usage_skips_empty(tmp_path, monkeypatch):
    log = tmp_path / "llm.jsonl"
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(log))
    lb._log_token_usage("m", {})
    lb._log_token_usage("m", {"prompt_tokens": 0, "completion_tokens": 0})
    assert not log.exists() or log.read_text().strip() == ""


# ─── compare_runs rollup ──────────────────────────────────────────────

def _run(tmp_path, llm_rows):
    d = tmp_path / "2026-01-01T00-00-00__vT__case"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text(
        json.dumps({"tick": 1, "t": 1.0}), encoding="utf-8")
    (d / "llm-log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in llm_rows), encoding="utf-8")
    return d


def test_metrics_sums_tokens(tmp_path):
    rows = [
        {"event": "token_usage", "role": "a", "model": "m",
         "prompt_tokens": 100, "completion_tokens": 20},
        {"event": "token_usage", "role": "b", "model": "m",
         "prompt_tokens": 50, "completion_tokens": 10},
        {"event": "call_start"},
    ]
    m = cr.metrics(_run(tmp_path, rows))
    assert m["tokens_prompt"] == 150 and m["tokens_completion"] == 30
    assert m["tokens_total"] == 180


def test_metrics_zero_tokens_when_no_usage(tmp_path):
    m = cr.metrics(_run(tmp_path, [{"event": "call_start"}]))
    assert m["tokens_total"] == 0 and m["tokens_per_node"] == 0.0


# ─── dashboard role+model rollup ──────────────────────────────────────

def test_token_economics_rolls_up_by_role_model(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    rows = [
        {"event": "token_usage", "role": "impl", "model": "haiku",
         "prompt_tokens": 100, "completion_tokens": 40},
        {"event": "token_usage", "role": "impl", "model": "haiku",
         "prompt_tokens": 60, "completion_tokens": 20},
        {"event": "token_usage", "role": "verifier", "model": "free",
         "prompt_tokens": 30, "completion_tokens": 10},
    ]
    (d / "llm-log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    econ = dash._token_economics(d)
    assert econ["total"] == 260      # 220 (impl/haiku) + 40 (verifier/free)
    top = econ["rows"][0]
    assert top["role"] == "impl" and top["model"] == "haiku"
    assert top["calls"] == 2 and top["total"] == 220


def test_token_economics_empty_without_log(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    assert dash._token_economics(d) == {"rows": [], "total": 0}
    assert dash._token_economics(None) == {"rows": [], "total": 0}
