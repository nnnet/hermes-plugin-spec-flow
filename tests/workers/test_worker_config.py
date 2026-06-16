"""Per-role worker configuration from the case YAML `workers:` block.

The block is the SINGLE source of provider/model truth when present:
providers form the available pool (lists with parameters), each role
carries an ordered model chain (primary + quota fallbacks), and every
chain entry must match a declared provider."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb        # noqa: E402


@pytest.fixture(autouse=True)
def clean_cfg():
    lb.configure_workers(None)
    yield
    lb.configure_workers(None)


CFG = {
    "providers": [
        {"name": "openrouter-free", "kind": "openai",
         "model_prefix": "openrouter/", "require_suffix": ":free"},
        {"name": "claude-cli", "kind": "claude", "model_prefix": "claude/"},
    ],
    "defaults": {"models": ["openrouter/qwen/qwen3-coder:free",
                            "claude/haiku"]},
    "reviewer": {"models": ["openrouter/meta-llama/llama-3.3:free",
                            "claude/haiku"]},
}


def test_chain_resolution_role_over_defaults():
    lb.configure_workers(CFG)
    assert lb.chain_for("reviewer")[0] == "openrouter/meta-llama/llama-3.3:free"
    assert lb.chain_for("implementer")[0] == "openrouter/qwen/qwen3-coder:free"
    assert lb.chain_for("implementer")[1] == "claude/haiku"
    assert lb.model_for("reviewer") == lb.chain_for("reviewer")[0]


def test_yaml_block_beats_env(monkeypatch):
    # models come STRICTLY from the case YAML when the block exists
    monkeypatch.setenv("SPEC_FLOW_REVIEWER_MODEL", "openrouter/other:free")
    lb.configure_workers(CFG)
    assert lb.model_for("reviewer") == "openrouter/meta-llama/llama-3.3:free"


def test_env_applies_without_block(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REVIEWER_MODEL", "openrouter/other:free")
    assert lb.model_for("reviewer") == "openrouter/other:free"


def test_scalar_model_reads_as_one_element_chain():
    lb.configure_workers({"defaults": {"model": "openrouter/solo:free"}})
    assert lb.chain_for("implementer") == ["openrouter/solo:free"]


def test_model_outside_provider_pool_rejected():
    bad = dict(CFG, defaults={"models": ["openai/gpt-4o"]})
    lb.configure_workers(bad)
    with pytest.raises(ValueError, match="matches no provider"):
        lb.chain_for("implementer")


def test_provider_suffix_requirement_enforced():
    # an openrouter model WITHOUT ':free' must not pass the pool
    bad = dict(CFG, defaults={"models": ["openrouter/qwen/qwen3-coder"]})
    lb.configure_workers(bad)
    with pytest.raises(ValueError, match="matches no provider"):
        lb.chain_for("implementer")


def test_ask_walks_chain_on_quota_exhaustion(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    calls = []

    def fake_openai(prompt, model, system=None):
        calls.append(("openai", model))
        raise lb.QuotaExhausted("429")

    def fake_claude(prompt, model, system=None, direct=False, timeout=None):
        calls.append(("claude", model, direct))
        return "chain answer"

    monkeypatch.setattr(lb, "_ask_openai", fake_openai)
    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    out = lb.ask("q", model="openrouter/a:free",
                 fallbacks=["claude/haiku"], role="decomposer", step="")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    assert out == "chain answer"
    assert calls == [("openai", "openrouter/a:free"),
                     ("claude", "haiku", True)]
    assert lb.last_call == {"backend": "claude", "model": "haiku",
                            "fallback": True}


def test_explicit_claude_model_bypasses_free_gate(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")

    def fake_claude(prompt, model, system=None, direct=False, timeout=None):
        return f"{model} direct={direct}"

    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    assert lb.ask("q", model="claude/haiku", role="decomposer", step="") == "haiku direct=True"


def test_paid_model_still_forbidden(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    with pytest.raises(ValueError, match="forbidden"):
        lb.ask("q", model="openrouter/gpt-4o", role="decomposer", step="")


# ─── run-wide LLM-call budget (quota limiter, off by default) ─────────

def _stub_ok(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "_ask_openai",
                        lambda prompt, model, system=None: "ok")


def test_budget_off_by_default(monkeypatch):
    _stub_ok(monkeypatch)
    for _ in range(5):
        assert lb.ask("q", model="openrouter/a:free", role="decomposer", step="") == "ok"
    assert lb.calls_made() == 5


def test_budget_from_workers_block(monkeypatch):
    _stub_ok(monkeypatch)
    lb.configure_workers({"budget": 2})
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    with pytest.raises(lb.BudgetExhausted):
        lb.ask("q", model="openrouter/a:free", role="decomposer", step="")


def test_budget_from_env_without_block(monkeypatch):
    _stub_ok(monkeypatch)
    monkeypatch.setenv("SPEC_FLOW_LLM_BUDGET", "1")
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    with pytest.raises(lb.BudgetExhausted):
        lb.ask("q", model="openrouter/a:free", role="decomposer", step="")


def test_chain_attempts_each_spend_budget(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)

    def exhausted(prompt, model, system=None):
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_openai", exhausted)
    monkeypatch.setattr(
        lb, "_ask_claude",
        lambda prompt, model, system=None, direct=False, timeout=None: "ok")
    lb.configure_workers({"budget": 10})
    lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"], role="decomposer", step="")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    assert lb.calls_made() == 2          # one free attempt + one fallback


def test_configure_resets_budget_counter(monkeypatch):
    _stub_ok(monkeypatch)
    lb.configure_workers({"budget": 3})
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    assert lb.calls_made() == 1
    lb.configure_workers({"budget": 3})
    assert lb.calls_made() == 0


def test_mixed_429_raises_quota_for_fallback(monkeypatch):
    # one 429 among the retries is a quota signal — the old `all retries
    # throttled` rule let a final 429 surface as RuntimeError and killed
    # a whole live run with no fallback
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "BACKOFF", 0.0)
    answers = [(500, "boom"), (429, '{"retry_after_seconds": 0}'),
               (502, "bad gateway")]
    monkeypatch.setattr(lb, "_http_post",
                        lambda url, payload, headers: answers.pop(0))
    with pytest.raises(lb.QuotaExhausted, match="throttled"):
        lb._ask_openai("q", "openrouter/a:free")


def test_chain_absorbs_plain_provider_failure(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)

    def broken(prompt, model, system=None):
        raise RuntimeError("openai backend failed after 3 tries: HTTP 500")

    monkeypatch.setattr(lb, "_ask_openai", broken)
    monkeypatch.setattr(
        lb, "_ask_claude",
        lambda prompt, model, system=None, direct=False, timeout=None: "fallback answer")
    out = lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"], role="decomposer", step="")
    assert out == "fallback answer"


def test_paid_gate_error_still_aborts_the_chain(monkeypatch):
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    with pytest.raises(ValueError, match="forbidden"):
        lb.ask("q", model="openrouter/gpt-4o", fallbacks=["claude/haiku"], role="decomposer", step="")


def test_exhausted_chain_waits_then_recovers(monkeypatch):
    # v18 death class: free pool out + terminal fallback capped killed
    # the RUN. With quota_retries the chain sleeps and tries again.
    lb.configure_workers({"quota_wait_s": 0.01, "quota_retries": 2,
                          "providers": [{"name": "openrouter-free",
                                         "kind": "openai",
                                         "model_prefix": "openrouter/",
                                         "require_suffix": ":free"}]})
    monkeypatch.setattr(lb, "FALLBACK_MODEL", None)
    calls = {"n": 0}

    def flaky(prompt, m, system, fallback=False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise lb.QuotaExhausted("429")
        return "recovered"

    monkeypatch.setattr(lb, "_ask_one", flaky)
    try:
        assert lb.ask("q", model="openrouter/a:free",
                      fallbacks=["openrouter/b:free"], role="decomposer", step="") == "recovered"
        assert calls["n"] == 3, "two failures absorbed by one wait round"
    finally:
        lb.configure_workers(None)


def test_exhausted_chain_raises_without_optin(monkeypatch):
    # default stays fail-fast: tests and ad-hoc calls never sleep
    lb.configure_workers({"providers": [{"name": "openrouter-free",
                                         "kind": "openai",
                                         "model_prefix": "openrouter/",
                                         "require_suffix": ":free"}]})
    monkeypatch.setattr(lb, "FALLBACK_MODEL", None)

    def dead(prompt, m, system, fallback=False):
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_one", dead)
    try:
        import pytest as _pt
        with _pt.raises(lb.QuotaExhausted):
            lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    finally:
        lb.configure_workers(None)


def test_hung_claude_cli_is_retriable_not_fatal(monkeypatch):
    # v19 death class: the terminal claude CLI hung and subprocess
    # raised TimeoutExpired, which slipped past the chain's exception
    # net and killed the run while OpenRouter was healthy.
    import subprocess as _sp
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        raise _sp.TimeoutExpired(cmd="claude", timeout=k.get("timeout", 1))

    monkeypatch.setattr(lb.subprocess, "run", fake_run)
    monkeypatch.setattr(lb, "RETRIES", 2)
    import pytest as _pt
    with _pt.raises(RuntimeError, match="timed out"):
        lb._ask_claude("q", "haiku", direct=True, timeout=1)
    assert calls["n"] == 2, "a hung CLI must be retried, then raise (not escape)"


def test_terminal_fallback_only_on_last_round(monkeypatch):
    # free-models policy: claude is the LAST resort — earlier rounds
    # wait and retry the free chain, sparing the weekly subscription cap
    lb.configure_workers({"quota_wait_s": 0.01, "quota_retries": 2,
                          "providers": [{"name": "openrouter-free",
                                         "kind": "openai",
                                         "model_prefix": "openrouter/",
                                         "require_suffix": ":free"}]})
    monkeypatch.setattr(lb, "FALLBACK_MODEL", "haiku")
    fb_calls = {"n": 0}

    def router(prompt, m, system, fallback=False, timeout=None):
        # the terminal fallback rotation calls _ask_one with fallback=True
        if fallback:
            fb_calls["n"] += 1
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_one", router)
    try:
        import pytest as _pt
        with _pt.raises((lb.QuotaExhausted, RuntimeError)):
            lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
        # 3 rounds (1 + 2 retries), the fallback rotation runs on the
        # LAST round only — sparing the capped provider on earlier rounds
        assert fb_calls["n"] == 1, \
            f"fallback must run once (last round), saw {fb_calls['n']}"
    finally:
        lb.configure_workers(None)


def test_fallback_rotation_cycles_across_providers(monkeypatch):
    # user ask: on exhaustion, ROTATE through providers (claude/haiku ->
    # openrouter_custom -> ...) instead of dying on one terminal model
    lb.configure_workers({
        "quota_wait_s": 0.001, "quota_retries": 0, "quota_wait_jitter": 0,
        "fallback_models": ["claude/haiku", "openrouter_custom/sonnet"],
        "providers": [{"name": "openrouter-free", "kind": "openai",
                       "model_prefix": "openrouter/",
                       "require_suffix": ":free"}]})
    tried = []

    def router(prompt, m, system, fallback=False, timeout=None):
        if fallback:
            tried.append(m)
            if m == "openrouter_custom/sonnet":
                return "answered by the second provider"
            raise lb.QuotaExhausted("haiku capped")
        raise lb.QuotaExhausted("free pool out")

    monkeypatch.setattr(lb, "_ask_one", router)
    try:
        out = lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
        assert out == "answered by the second provider"
        # first provider tried and capped, rolled to the second
        assert tried == ["claude/haiku", "openrouter_custom/sonnet"]
    finally:
        lb.configure_workers(None)


def test_fallback_rotation_start_advances_each_round(monkeypatch):
    # a capped provider must not be retried FIRST every round — the
    # rotation start advances so the other provider leads next time
    lb.configure_workers({
        "quota_wait_s": 0.001, "quota_retries": 1, "quota_wait_jitter": 0,
        "fallback_models": ["claude/haiku", "openrouter_custom/sonnet"],
        "providers": [{"name": "openrouter-free", "kind": "openai",
                       "model_prefix": "openrouter/",
                       "require_suffix": ":free"}]})
    order = []

    def router(prompt, m, system, fallback=False, timeout=None):
        if fallback:
            order.append(m)
        raise lb.QuotaExhausted("all capped")

    monkeypatch.setattr(lb, "_ask_one", router)
    try:
        import pytest as _pt
        with _pt.raises((lb.QuotaExhausted, RuntimeError)):
            lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
        # rounds=2 -> last round attempt=1, start = 1 % 2 = 1: the lead
        # ADVANCES to the second provider rather than always retrying the
        # capped first one; then it wraps to cover both
        assert order == ["openrouter_custom/sonnet", "claude/haiku"]
    finally:
        lb.configure_workers(None)


def test_jitter_spreads_the_wait(monkeypatch):
    # thundering-herd guard: the wait is NOT a fixed value when jitter on
    cfg = {"quota_wait_jitter": 0.2}
    seen = {lb._jittered(100.0, cfg) for _ in range(20)}
    assert len(seen) > 1, "jitter must vary the wait"
    assert all(80 <= v <= 120 for v in seen), "jitter stays within +/-20%"
    # jitter 0 is deterministic (tests rely on this)
    assert lb._jittered(100.0, {"quota_wait_jitter": 0}) == 100.0


def test_rotation_kicks_in_after_fallback_after_rounds(monkeypatch):
    # v21 wedge: waiting ALL 11 rounds before trying the haiku rotation
    # left the run stuck ~55 min on a hard free-pool cooldown. With
    # fallback_after_rounds=1 the rotation must fire from round 1, not
    # only the last round.
    lb.configure_workers({
        "quota_wait_s": 0.001, "quota_retries": 3, "quota_wait_jitter": 0,
        "fallback_after_rounds": 1,
        "fallback_models": ["claude/haiku"],
        "providers": [{"name": "openrouter-free", "kind": "openai",
                       "model_prefix": "openrouter/",
                       "require_suffix": ":free"}]})
    fb_rounds = []
    state = {"round": -1}

    def router(prompt, m, system, fallback=False, timeout=None):
        if not fallback:
            state["round"] += 1
            raise lb.QuotaExhausted("free pool out")
        fb_rounds.append(state["round"])
        return "haiku saved it"

    monkeypatch.setattr(lb, "_ask_one", router)
    try:
        out = lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
        assert out == "haiku saved it"
        # rotation fired on round 1 (the second round), not waiting for 3
        assert fb_rounds and fb_rounds[0] == 1
    finally:
        lb.configure_workers(None)


def test_stages_block_flattens_to_role_keys():
    # The clean spec-pipeline shape: workers.stages with verb names. It must
    # flatten onto the internal -er role keys the engine resolves by.
    try:
        lb.configure_workers({
            "defaults": {"models": ["d/x"]},
            "stages": {
                "decompose": {"models": ["openrouter/dec:free"]},
                "implement": {"models": ["openrouter/imp:free"],
                              "team": {"specialists": [{"role": "coder"}]}},
                "review": {"models": ["openrouter/rev:free"]},
                "verify": {"models": ["openrouter/ver:free"]},
            },
        })
        assert lb.model_for("decomposer") == "openrouter/dec:free"
        assert lb.model_for("implementer") == "openrouter/imp:free"
        assert lb.model_for("reviewer") == "openrouter/rev:free"
        assert lb.model_for("verifier") == "openrouter/ver:free"
        # the implement team survives the flatten
        assert lb.WORKERS_CFG["implementer"]["team"]["specialists"][0]["role"] == "coder"
        # the verb key itself is consumed, not left dangling
        assert "stages" not in lb.WORKERS_CFG
    finally:
        lb.configure_workers(None)


def test_flat_key_wins_over_stage_of_same_meaning():
    # An explicit flat key must not be clobbered by a stage alias.
    try:
        lb.configure_workers({
            "decomposer": {"models": ["flat/wins"]},
            "stages": {"decompose": {"models": ["stage/loses"]}},
        })
        assert lb.model_for("decomposer") == "flat/wins"
    finally:
        lb.configure_workers(None)


def test_ask_logs_token_usage_at_exit(monkeypatch, tmp_path):
    # Token accounting is universal at ask()'s exit: real usage when a backend
    # stashed one, tiktoken estimate otherwise — every successful ask logs one
    # token_usage row (regression: the old per-backend log never fired live).
    import json as _json
    from harness import llm_log
    logf = tmp_path / "ll.jsonl"
    monkeypatch.setattr(llm_log, "LOG_PATH", str(logf), raising=False)
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(logf))

    def fake_real(prompt, model, system, params=None, **k):
        lb._call_ctx.last_usage = {"prompt_tokens": 11, "completion_tokens": 7}
        return "real"
    def fake_none(prompt, model, system, params=None, **k):
        lb._call_ctx.last_usage = None
        return "estimated body"
    try:
        lb.configure_workers({"backend": "openai", "base_url": "x"})
        monkeypatch.setattr(lb, "_ask_one", fake_real)
        lb.ask("hello", model="openrouter/m:free", role="decomposer", step="")
        monkeypatch.setattr(lb, "_ask_one", fake_none)
        lb.ask("count me", model="openrouter/m:free", role="coder", step="coder")
    finally:
        lb.configure_workers(None)

    rows = [_json.loads(l) for l in logf.read_text().splitlines()
            if '"token_usage"' in l]
    assert len(rows) == 2, rows
    real, est = rows[0], rows[1]
    assert real["estimated"] is False and real["prompt_tokens"] == 11
    assert est["estimated"] is True and est["completion_tokens"] > 0
    assert est["step"] == "coder"


def test_paid_guard_allows_free_provider_model(monkeypatch):
    # A model routed to a provider declared with a free daily quota
    # (requests_per_day) must be allowed even without a ':free' suffix —
    # e.g. xiaomimimo. A genuine paid model is still rejected.
    try:
        lb.configure_workers({
            "backend": "openai", "base_url": "x",
            "providers": [
                {"name": "xiaomimimo", "kind": "openai",
                 "model_prefix": "xiaomimimo/", "requests_per_day": 1000},
                {"name": "orf", "kind": "openai", "model_prefix": "openrouter/",
                 "require_suffix": ":free", "requests_per_day": 1000},
            ],
        })
        assert lb._is_free_model("xiaomimimo/mimo-v2.5") is True
        assert lb._is_free_model("openrouter/qwen/qwen3-coder:free") is True
        # a model that matches no free provider and lacks ':free' is not free
        assert lb._is_free_model("openrouter/qwen/qwen3-coder") is False
    finally:
        lb.configure_workers(None)


def test_ask_requires_role_and_step():
    # role + step are MANDATORY — a call without them must crash (so a missing
    # attribution can never silently produce an empty-stage token row).
    import pytest as _pytest
    try:
        lb.configure_workers({"backend": "openai", "base_url": "x"})
        with _pytest.raises(TypeError):
            lb.ask("q", model="claude/haiku", step="")        # role missing
        with _pytest.raises(TypeError):
            lb.ask("q", model="claude/haiku", role="reviewer")  # step missing
        with _pytest.raises(ValueError):
            lb.ask("q", model="claude/haiku", role="", step="")  # empty role
    finally:
        lb.configure_workers(None)


# ─── full LLM-call outcome logging (Part 1) ──────────────────────────────

def test_ask_openai_logs_every_attempt(monkeypatch):
    # every HTTP attempt is logged so the dashboard can show ALL delay causes:
    # a 429 (abnormal, with the backoff it slept) and the final 200.
    from harness import llm_log
    events = []
    monkeypatch.setattr(llm_log, "log", lambda e: events.append(e))
    monkeypatch.setattr(lb, "BACKOFF", 0.01)
    monkeypatch.setattr(lb, "RETRIES", 3)
    seq = [(429, '{"error":"rate limited"}'),
           (200, '{"choices":[{"message":{"content":"hi"}}]}')]
    monkeypatch.setattr(lb, "_http_post", lambda url, p, h: seq.pop(0))
    out = lb._ask_openai("q", "openrouter/a:free")
    assert out == "hi"
    att = [e for e in events if e.get("event") == "llm_attempt"]
    assert any(e["status"] == 429 and e["abnormal"] and "slept_s" in e for e in att)
    assert any(e["status"] == 200 and not e["abnormal"] for e in att)
    assert all(e["provider"] == "openrouter" for e in att)


def test_ask_openai_logs_empty_reply_as_abnormal(monkeypatch):
    from harness import llm_log
    events = []
    monkeypatch.setattr(llm_log, "log", lambda e: events.append(e))
    monkeypatch.setattr(lb, "BACKOFF", 0.0)
    monkeypatch.setattr(lb, "RETRIES", 1)
    # HTTP 200 but no usable content/reasoning → abnormal, error=empty
    monkeypatch.setattr(lb, "_http_post",
                        lambda url, p, h: (200, '{"choices":[{"message":{"content":""}}]}'))
    with pytest.raises((lb.QuotaExhausted, RuntimeError)):
        lb._ask_openai("q", "openrouter/a:free")
    att = [e for e in events if e.get("event") == "llm_attempt"]
    assert any(e["status"] == 200 and e["abnormal"] and e["error"] == "empty" for e in att)


def test_ask_logs_fallback_transition_and_terminal(monkeypatch):
    from harness import llm_log
    events = []
    monkeypatch.setattr(llm_log, "log", lambda e: events.append(e))
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)

    def fake_openai(prompt, model, system=None):
        raise lb.QuotaExhausted("429")

    def fake_claude(prompt, model, system=None, direct=False, timeout=None):
        return "chain answer"

    monkeypatch.setattr(lb, "_ask_openai", fake_openai)
    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    out = lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"],
                 role="decomposer", step="")
    assert out == "chain answer"
    fb = [e for e in events if e.get("event") == "llm_fallback"]
    assert any(e["from_model"] == "openrouter/a:free"
               and e["to_model"] == "claude/haiku"
               and e["reason"] == "429" and not e["terminal"] for e in fb)


def test_ask_logs_terminal_when_all_models_fail(monkeypatch):
    from harness import llm_log
    events = []
    monkeypatch.setattr(llm_log, "log", lambda e: events.append(e))
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "_jittered", lambda w, c: 0.0)

    def boom_openai(prompt, model, system=None):
        raise lb.QuotaExhausted("429")

    def boom_claude(prompt, model, system=None, direct=False, timeout=None):
        raise lb.QuotaExhausted("429")

    monkeypatch.setattr(lb, "_ask_openai", boom_openai)
    monkeypatch.setattr(lb, "_ask_claude", boom_claude)
    with pytest.raises(lb.QuotaExhausted):
        lb.ask("q", model="openrouter/a:free", fallbacks=["claude/haiku"],
               role="decomposer", step="")
    fb = [e for e in events if e.get("event") == "llm_fallback"]
    assert any(e["terminal"] and e["to_model"] is None for e in fb)
