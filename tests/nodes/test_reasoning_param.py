"""Reasoning control: the abstract `params.reasoning` knob ("off"|"low"|
"medium"|"high") must be translated into each provider's REAL wire form at the
single request-body seam (llm_backend._ask_openai), degrade honestly where a
provider cannot truly disable thinking (system nudge + think-block strip), and
change NOTHING when the key is absent. All through the real HTTP path against
a real local OpenAI-compatible server (fake_openai fixture, no monkeypatch of
our code)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb  # noqa: E402
from harness_fakeapi import ok          # noqa: E402

# providers registry so xiaomimimo/openrouter models pass the paid-model guard
_PROVIDERS = [
    {"name": "xiaomimimo", "kind": "openai", "model_prefix": "xiaomimimo/",
     "requests_per_day": 1000},
    {"name": "openrouter", "kind": "openai", "model_prefix": "openrouter/",
     "requests_per_day": 1000},
]


def _system_text(payload: dict) -> str:
    return "\n".join(m["content"] for m in payload.get("messages", [])
                     if m.get("role") == "system")


# ── (a) translation reaches the request body per provider ──────────────────

def test_reasoning_off_xiaomimimo_wire_form(fake_openai):
    """xiaomimimo has NO true off (experiment 2026-07-02): "off" must send the
    cheapest legal effort AND the universal off-fallback nudge; the raw
    `reasoning` key never reaches the wire."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    out = lb.ask("q", model="xiaomimimo/mimo-v2.5",
                 params={"reasoning": "off"}, role="reviewer", step="")
    assert out == "ok"
    payload = srv.requests[0]["payload"]
    assert payload["reasoning_effort"] == "low"
    assert "reasoning" not in payload
    assert lb._NO_REASONING_NUDGE in _system_text(payload)


def test_reasoning_off_openrouter_wire_form(fake_openai):
    """OpenRouter supports a true disable: nested {"reasoning": {"enabled":
    false}}; no reasoning_effort key leaks in."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    lb.ask("q", model="openrouter/some/model:free",
           params={"reasoning": "off"}, role="reviewer", step="")
    payload = srv.requests[0]["payload"]
    assert payload["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in payload


def test_reasoning_level_maps_without_nudge(fake_openai):
    """A non-off level translates to the provider's effort value and does NOT
    inject the no-reasoning nudge (thinking stays on, just bounded)."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    lb.ask("q", model="xiaomimimo/mimo-v2.5",
           params={"reasoning": "high"}, role="implementer", step="")
    payload = srv.requests[0]["payload"]
    assert payload["reasoning_effort"] == "high"
    assert lb._NO_REASONING_NUDGE not in _system_text(payload)


def test_stage_level_params_reach_the_body(fake_openai):
    """workers.stages.review.params (flattened to the `reviewer` role key by
    configure_workers) must ride into an ask(role='reviewer') call with no
    explicit caller params — the p6 YAML mechanism."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS,
                          "stages": {"review": {"params": {"reasoning": "off"}}}})
    lb.ask("q", model="xiaomimimo/mimo-v2.5", role="reviewer", step="")
    payload = srv.requests[0]["payload"]
    assert payload["reasoning_effort"] == "low"
    assert lb._NO_REASONING_NUDGE in _system_text(payload)


def test_yaml_false_footgun_means_off(fake_openai):
    """Bare YAML `off` parses as boolean False — the backend must read it as
    the string level "off", not drop it."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    lb.ask("q", model="xiaomimimo/mimo-v2.5",
           params={"reasoning": False}, role="verifier", step="")
    assert srv.requests[0]["payload"]["reasoning_effort"] == "low"


# ── (b) absent key changes nothing ──────────────────────────────────────────

def test_absent_reasoning_key_leaves_body_untouched(fake_openai):
    """No `reasoning` in params -> no translated key in the body, no nudge in
    the messages — byte-for-byte today's request."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    lb.ask("q", model="xiaomimimo/mimo-v2.5",
           params={"temperature": 0.3}, role="reviewer", step="")
    payload = srv.requests[0]["payload"]
    assert "reasoning_effort" not in payload
    assert "reasoning" not in payload
    assert lb._NO_REASONING_NUDGE not in _system_text(payload)


def test_unknown_reasoning_value_is_ignored(fake_openai):
    """A typo level must never 400 a live call: nothing is sent for it."""
    srv = fake_openai([(200, ok("ok"))])
    lb.configure_workers({"providers": _PROVIDERS})
    lb.ask("q", model="xiaomimimo/mimo-v2.5",
           params={"reasoning": "bogus"}, role="reviewer", step="")
    payload = srv.requests[0]["payload"]
    assert "reasoning_effort" not in payload and "reasoning" not in payload


# ── (c) think-block strip on "off" ───────────────────────────────────────────

def test_think_block_stripped_when_off(fake_openai):
    """With reasoning "off" an inline <think> block in the reply is cut before
    the caller sees it — strict-JSON consumers get clean text."""
    srv = fake_openai(
        [(200, ok("<think>let me ponder…</think>{\"verdict\": \"PASS\"}"))])
    lb.configure_workers({"providers": _PROVIDERS})
    out = lb.ask("q", model="xiaomimimo/mimo-v2.5",
                 params={"reasoning": "off"}, role="reviewer", step="")
    assert out == '{"verdict": "PASS"}'
    assert srv.call_count == 1


def test_think_block_kept_without_the_knob(fake_openai):
    """No reasoning key -> the reply is returned verbatim (legacy behaviour),
    think block included."""
    raw = "<think>hm</think>answer"
    fake_openai([(200, ok(raw))])
    lb.configure_workers({"providers": _PROVIDERS})
    out = lb.ask("q", model="xiaomimimo/mimo-v2.5",
                 role="reviewer", step="")
    assert out == raw


# ── unit edges: normaliser + stripper ────────────────────────────────────────

def test_reasoning_level_normaliser():
    assert lb._reasoning_level(None) == ""
    assert lb._reasoning_level({}) == ""
    assert lb._reasoning_level({"reasoning": None}) == ""
    assert lb._reasoning_level({"reasoning": False}) == "off"
    assert lb._reasoning_level({"reasoning": "OFF"}) == "off"
    assert lb._reasoning_level({"reasoning": " High "}) == "high"
    assert lb._reasoning_level({"reasoning": "bogus"}) == ""


def test_strip_think_edges():
    assert lb._strip_think("<think>x</think>ok") == "ok"
    assert lb._strip_think("ok") == "ok"
    # dangling unclosed tail is cut only when real text precedes it
    assert lb._strip_think("ok<think>trunc") == "ok"
    # stripping must never EMPTY a reply — the answer may live inside the block
    assert lb._strip_think("<think>only thoughts") == "<think>only thoughts"
    assert lb._strip_think("<THINK>a</THINK>b<think>c</think>d") == "bd"
