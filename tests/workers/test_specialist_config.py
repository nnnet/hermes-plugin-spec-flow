"""Phase-1 of the roles->executors->specialists->providers architecture
(.claude/plans/2026-06-16T07-35__orchestra-specialist-provider-architecture.md):
per-specialist model + open-schema params, the `local` provider seam, and the
RoleTask/RoleResult contract — all WITHOUT changing observable behaviour.

Hermetic: no live LLM, no network, no pytest subprocess. The orchestra's
building blocks are stubbed and the model door is a fake that CAPTURES the
kwargs it receives, so we can assert a specialist's model/params actually reach
the backend call."""
import contextlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb    # noqa: E402


@contextlib.contextmanager
def _noop_tx(*a, **k):
    yield object()


def _base_ctx(tmp_path):
    ws = type("WS", (), {"root": str(tmp_path),
                         "_write": lambda self, *a, **k: None})()
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": ws, "spec": "specs/leaf1.md", "module": "leaf1",
            "specialty": ""}


# ── config parser: canonical `specialists` + alias list + provider/params ──

def test_specialists_block_is_canonical():
    """`team: {specialists: [...]}` is the canonical key; each specialist
    carries its own provider (default local), model and params."""
    lb.configure_workers({"implementer": {"team": {"specialists": [
        {"role": "architect", "model": "free/a:free",
         "params": {"temperature": 0.2}},
        {"role": "coder", "model": "free/c:free",
         "params": {"temperature": 0.4, "max_tokens": 4000}},
        {"role": "tester"},
        {"role": "fixer"}]}}})
    team = rw._implementer_team()
    lb.configure_workers(None)
    assert [s["role"] for s in team] == \
        ["architect", "coder", "tester", "fixer"]
    assert team[0]["provider"] == "local"          # default
    assert team[0]["model"] == "free/a:free"
    assert team[0]["params"] == {"temperature": 0.2}
    assert team[1]["params"] == {"temperature": 0.4, "max_tokens": 4000}
    assert team[2]["model"] is None                 # no model -> chain fallback
    assert team[2]["params"] is None


def test_old_list_form_still_parses():
    """The legacy bare list `team: [{role}...]` keeps working as an alias for
    `specialists:`, with provider defaulting to local."""
    lb.configure_workers({"implementer": {"team": [
        {"role": "coder"}, {"role": "fixer", "model": "free/x:free"}]}})
    team = rw._implementer_team()
    lb.configure_workers(None)
    assert [s["role"] for s in team] == ["coder", "fixer"]
    assert team[0]["provider"] == "local"
    assert team[1]["model"] == "free/x:free"


def test_specialists_env_override_json(monkeypatch):
    """SPEC_FLOW_IMPLEMENTER_TEAM may carry the canonical dict shape too."""
    lb.configure_workers(None)
    monkeypatch.setenv("SPEC_FLOW_IMPLEMENTER_TEAM", json.dumps(
        {"specialists": [{"role": "coder", "params": {"top_p": 0.9}}]}))
    team = rw._implementer_team()
    assert [s["role"] for s in team] == ["coder"]
    assert team[0]["params"] == {"top_p": 0.9}


def test_known_provider_resolves(monkeypatch):
    """A specialist on a registered remote provider resolves to that provider
    NAME (the adapter exists); only an UNKNOWN provider fails."""
    assert rw._resolve_provider({"role": "coder", "provider": "hermes"}) == "hermes"
    assert rw._resolve_provider({"role": "coder"}) == "local"


def test_unknown_provider_raises(monkeypatch):
    """An unregistered provider fails LOUDLY at resolution, naming the available
    ones — it must never be silently skipped."""
    with pytest.raises(ValueError) as exc:
        rw._resolve_provider({"role": "coder", "provider": "bogus"})
    assert "bogus" in str(exc.value)


# ── per-specialist model + params reach the backend call ──────────────────

def _stub_orchestra_machinery(monkeypatch):
    """Stub the heavy orchestra building blocks so a step runs as a single
    captured model call with no git/pytest/file I/O."""
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "chainhead:free")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "chainhead:free")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC BODY")
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: (True, "green"))


def test_specialist_model_and_params_reach_backend(monkeypatch, tmp_path):
    """A coder specialist with its own model + params: the model used for its
    step is the declared one (not the role chain head), and the params dict is
    threaded all the way down to llm_backend.ask."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _stub_orchestra_machinery(monkeypatch)

    captured = {}

    def fake_ask(prompt, *, model, system=None, fallbacks=(), role="",
                 step="", params=None):
        captured["model"] = model
        captured["params"] = params
        return json.dumps({"files": {"src/leaf1.py": "x = 1\n"}})

    # the coder goes through _ensemble_generate -> _dialog_round -> _call_model
    # -> (chat-only) llm_backend.ask; force the chat path and stub ask.
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(rw.llm_backend, "chain_for",
                        lambda role, specialty="": ["chainhead:free"])
    monkeypatch.setattr(rw.llm_backend, "ask", fake_ask)

    team = [{"role": "coder", "model": "free/coder:free",
             "params": {"temperature": 0.4, "max_tokens": 4000}}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    assert captured["model"] == "free/coder:free", \
        "the specialist's explicit model must be used for its step"
    assert captured["params"] == {"temperature": 0.4, "max_tokens": 4000}, \
        "the specialist's params must reach the backend call"


def test_specialist_without_model_falls_back_to_chain(monkeypatch, tmp_path):
    """A specialist with NO model uses the implementer role's chain head — the
    same resolver the single-agent path uses (today's behaviour)."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _stub_orchestra_machinery(monkeypatch)

    captured = {}

    def fake_ask(prompt, *, model, system=None, fallbacks=(), role="",
                 step="", params=None):
        captured["model"] = model
        captured["params"] = params
        return json.dumps({"files": {"src/leaf1.py": "x = 1\n"}})

    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(rw.llm_backend, "chain_for",
                        lambda role, specialty="": ["chainhead:free"])
    monkeypatch.setattr(rw.llm_backend, "ask", fake_ask)

    team = [{"role": "coder"}]            # no model, no params
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    assert captured["model"] == "chainhead:free", \
        "no specialist model -> the role chain head answers"
    assert captured["params"] is None, "no params -> backend defaults (no dict)"


# ── team-of-one equivalence + ordered multi-step run ──────────────────────

def test_team_of_one_runs_the_single_coder_step(monkeypatch, tmp_path):
    """A team of one coder produces exactly one generation step and verifies
    the leaf — the degenerate orchestra ≡ a single worker call."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _stub_orchestra_machinery(monkeypatch)

    gens = {"n": 0}

    def fake_ensemble(prompt, *, node, system, allowed, disallowed, cwd, model,
                      channel, specialty, meta=None, params=None):
        gens["n"] += 1
        return json.dumps({"files": {f"src/{node}.py": "x = 1\n"}})

    monkeypatch.setattr(rw, "_ensemble_generate", fake_ensemble)

    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=[{"role": "coder"}])
    assert gens["n"] == 1, "team-of-one runs exactly the single coder step"


def test_old_list_form_runs_all_steps_in_order(monkeypatch, tmp_path):
    """The legacy bare list `team: [{role}...]` still drives every step in
    declared order through the orchestra."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _stub_orchestra_machinery(monkeypatch)
    # coder/tester leave it RED so the fixer step has real work
    bar = iter([(False, "FAIL"), (False, "FAIL"), (True, "fixed")])
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: next(bar))

    order = []
    monkeypatch.setattr(rw, "_dialog_round",
                        lambda *a, **k: order.append("architect") or "PLAN")

    def fake_ensemble(prompt, *, node, **k):
        order.append("tester" if "TESTER PASS" in prompt else "coder")
        return json.dumps({"files": {f"src/{node}.py": "x = 1\n"}})

    monkeypatch.setattr(rw, "_ensemble_generate", fake_ensemble)
    monkeypatch.setattr(rw, "_call_model",
                        lambda *a, **k: order.append("fixer") or "no diff")
    monkeypatch.setattr(rw, "_apply_diff_repair", lambda *a, **k: True)

    # the OLD list shape, exactly as a pre-phase-1 case wrote it
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert order == ["architect", "coder", "tester", "fixer"]


# ── RoleTask / RoleResult contract (doc §3) ───────────────────────────────

def test_roletask_roundtrips_specialist_config():
    """The RoleTask the orchestra builds for a specialist carries its role,
    node, provider, resolved model and params — the uniform seam every (future)
    provider adapter will read."""
    lb.configure_workers(None)
    import harness.llm_backend as _lb
    _lb_model = _lb.model_for
    try:
        _lb.model_for = lambda *a, **k: "chainhead:free"
        ctx = {"title": "Leaf One", "spec": "specs/leaf1.md", "specialty": ""}
        step = {"role": "coder", "params": {"temperature": 0.4}}
        task = rw._specialist_task(step, ctx, "leaf1", "leaf1", "/ws")
    finally:
        _lb.model_for = _lb_model
    assert isinstance(task, rw.RoleTask)
    assert task.role == "coder"
    assert task.node == "leaf1"
    assert task.provider == "local"
    assert task.model == "chainhead:free"      # no step model -> chain head
    assert task.params == {"temperature": 0.4}
    assert task.context["module"] == "leaf1"


def test_roletask_remote_provider_resolves():
    """Building the RoleTask for a registered remote specialist carries that
    provider name (the adapter exists); an unknown provider raises."""
    lb.configure_workers(None)
    task = rw._specialist_task({"role": "coder", "provider": "a2a"},
                               {"specialty": ""}, "leaf1", "leaf1", "/ws")
    assert task.provider == "a2a"
    with pytest.raises(ValueError):
        rw._specialist_task({"role": "coder", "provider": "nope"},
                            {"specialty": ""}, "leaf1", "leaf1", "/ws")


def test_roleresult_shape():
    """RoleResult is the uniform outcome record: kind + artifacts + optional
    verdict + open-schema meta."""
    res = rw.RoleResult(kind="files",
                        artifacts={"src/x.py": "..."},
                        meta={"provider": "local", "step": "coder"})
    assert res.kind == "files"
    assert res.artifacts == {"src/x.py": "..."}
    assert res.verdict is None
    assert res.meta["provider"] == "local"


# ── backend: open-schema params -> OpenAI payload passlist ─────────────────

def test_openai_params_passlist():
    """Only recognised OpenAI sampling fields survive into the request body;
    an unknown knob is dropped so it can never 400 the call."""
    out = lb._openai_params({"temperature": 0.3, "max_tokens": 100,
                             "bogus_knob": 1, "stop": ["\n"]})
    assert out == {"temperature": 0.3, "max_tokens": 100, "stop": ["\n"]}
    assert lb._openai_params(None) == {}
    assert lb._openai_params({}) == {}


def test_ask_threads_params_into_openai_payload(monkeypatch):
    """ask(params={...}) must reach the OpenAI payload through the chain."""
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    lb.configure_workers(None)
    seen = {}

    def fake_post(url, payload, headers):
        seen["payload"] = payload
        return 200, json.dumps(
            {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(lb, "_http_post", fake_post)
    out = lb.ask("q", model="openrouter/a:free",
                 params={"temperature": 0.7, "max_tokens": 50}, role="decomposer", step="")
    assert out == "ok"
    assert seen["payload"]["temperature"] == 0.7
    assert seen["payload"]["max_tokens"] == 50


def test_ask_without_params_keeps_legacy_payload(monkeypatch):
    """No params -> the payload is exactly {model, messages} as before, so a
    paramless call is byte-for-byte the legacy request."""
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    lb.configure_workers(None)
    seen = {}

    def fake_post(url, payload, headers):
        seen["payload"] = payload
        return 200, json.dumps(
            {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(lb, "_http_post", fake_post)
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    assert set(seen["payload"]) == {"model", "messages"}
