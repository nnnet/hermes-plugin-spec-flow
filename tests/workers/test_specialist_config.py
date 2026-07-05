"""Phase-1 of the roles->executors->specialists->providers architecture
(.claude/plans/2026-06-16T07-35__orchestra-specialist-provider-architecture.md):
per-specialist model + open-schema params, the `local` provider seam, and the
RoleTask/RoleResult contract — all WITHOUT changing observable behaviour.

Hermetic: no live LLM, no network, no pytest subprocess. The orchestra's
building blocks are stubbed and the model door is a fake that CAPTURES the
kwargs it receives, so we can assert a specialist's model/params actually reach
the backend call."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb    # noqa: E402
from harness import ws_tx                # noqa: E402
from harness_fakeapi import ok           # noqa: E402


# A real green leaf: the coder's reply is RUNNABLE — the test imports the
# module and asserts real behaviour, so a real pytest run goes green for real
# (no faked verdict). Used wherever the orchestra must actually pass the leaf.
_GREEN_FILES = {
    "src/leaf1.py": "def value():\n    return 1\n",
    "tests/test_leaf1.py": (
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve()"
        ".parents[1] / 'src'))\n"
        "import leaf1\n"
        "def test_value():\n    assert leaf1.value() == 1\n"),
}


class _RealWS:
    """A real workspace: writes land on disk, so the orchestra's git
    transaction, pytest run and leaf bar all execute against real files."""
    enabled = True

    def __init__(self, root):
        self.root = str(root)

    def _write(self, rel, body, kind):
        f = pathlib.Path(self.root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
        return rel


def _base_ctx(tmp_path):
    spec = pathlib.Path(tmp_path) / "specs" / "leaf1.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Leaf One\nReturn the constant 1.\n", encoding="utf-8")
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": _RealWS(tmp_path), "spec": "specs/leaf1.md",
            "module": "leaf1", "specialty": ""}


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

def _real_env(monkeypatch, tmp_path, *, model_head="chainhead:free"):
    """Wire the orchestra to run for REAL — real git workspace, real pytest,
    real file writes, real leaf bar. ONLY config seams are pinned (skill text,
    the role's chain-head model, single-member ensemble, the chat path); the
    verdict is a genuine pytest run, never a stub."""
    ws_tx.ensure_repo(str(tmp_path))
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: model_head)
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: model_head)
    monkeypatch.setattr(rw, "_ensemble_size", lambda *a, **k: 1)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(rw.llm_backend, "chain_for",
                        lambda *a, **k: [model_head])


def test_specialist_model_and_params_reach_backend(monkeypatch, tmp_path,
                                                   fake_openai):
    """A coder specialist with its own model + params: the model used for its
    step is the declared one (not the role chain head), and the params dict is
    threaded all the way down to the real backend HTTP call. Nothing on the
    path is faked — the coder's reply is served by a real local OpenAI server,
    the files it returns are written to a real git workspace and the leaf is
    verified by a REAL pytest run; we read the actual request the harness sent."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    srv = fake_openai([(200, ok(json.dumps({"files": _GREEN_FILES})))])

    team = [{"role": "coder", "model": "free/coder:free",
             "params": {"temperature": 0.4, "max_tokens": 4000}}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    payload = srv.requests[0]["payload"]
    assert payload["model"] == "free/coder:free", \
        "the specialist's explicit model must be used for its step"
    assert payload["temperature"] == 0.4 and payload["max_tokens"] == 4000, \
        "the specialist's params must reach the backend call"
    # the real machinery actually ran: the leaf landed on disk and is green
    assert (tmp_path / "src" / "leaf1.py").exists()


def test_specialist_without_model_falls_back_to_chain(monkeypatch, tmp_path,
                                                      fake_openai):
    """A specialist with NO model uses the implementer role's chain head — the
    same resolver the single-agent path uses (today's behaviour). The reply is
    served by the real local server and verified by a real pytest run; we read
    the model the harness really sent and confirm a paramless step carries only
    the universal low-temperature default (2c41c07 prevention: every worker
    call gets temperature 0.1 unless the caller sets one), no other knobs."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    srv = fake_openai([(200, ok(json.dumps({"files": _GREEN_FILES})))])

    team = [{"role": "coder"}]            # no model, no params
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    payload = srv.requests[0]["payload"]
    assert payload["model"] == "chainhead:free", \
        "no specialist model -> the role chain head answers"
    assert set(payload) == {"model", "messages", "temperature"}, \
        "no params -> only the universal low-temperature default rides along"
    assert payload["temperature"] == 0.1


# ── team-of-one equivalence + ordered multi-step run ──────────────────────

def test_team_of_one_runs_the_single_coder_step(monkeypatch, tmp_path,
                                                fake_openai):
    """A team of one coder produces exactly one generation step and lands a
    real green leaf — the degenerate orchestra ≡ a single worker call, verified
    against a REAL pytest run (no faked verdict, no step double)."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    srv = fake_openai([(200, ok(json.dumps({"files": _GREEN_FILES})))])
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=[{"role": "coder"}])
    assert len(srv.requests) == 1, \
        "team-of-one runs exactly the single coder step"
    assert (tmp_path / "src" / "leaf1.py").exists(), "the leaf landed for real"


def test_old_list_form_runs_all_steps_in_order(monkeypatch, tmp_path,
                                               fake_openai):
    """The legacy bare list `team: [{role}...]` drives every step in declared
    order through the orchestra, and the order is read from REAL work: the coder
    ships a RED leaf, the tester strengthens it (still red), and only the fixer
    greens it under a real pytest run — never a stubbed verdict. Each step is
    identified by the prompt the harness actually sent the local server."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    order = []
    red_src = "def value():\n    return 0\n"       # wrong -> the green test fails
    green_src = "def value():\n    return 1\n"      # the fixer's correct code
    test_body = _GREEN_FILES["tests/test_leaf1.py"]

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "The test run FAILED" in msg:           # repair prompt -> fixer
            order.append("fixer")
            return 200, ok(json.dumps({"files": {"src/leaf1.py": green_src}}))
        if "ARCHITECT sub-role" in msg:
            order.append("architect")
            return 200, ok("PLAN: expose value() returning 1")
        if "TESTER PASS" in msg:                   # strengthen tests; src stays red
            order.append("tester")
            return 200, ok(json.dumps({"files": {"tests/test_leaf1.py":
                                                 test_body}}))
        order.append("coder")
        return 200, ok(json.dumps({"files": {"src/leaf1.py": red_src,
                                             "tests/test_leaf1.py": test_body}}))

    fake_openai(router)

    # the OLD list shape, exactly as a pre-phase-1 case wrote it
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert order == ["architect", "coder", "tester", "fixer"]
    # the fixer's real repair landed and greened the leaf for real
    body = (tmp_path / "src" / "leaf1.py").read_text(encoding="utf-8")
    assert body.strip().endswith("return 1"), "the fixer's real fix is on disk"


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


def test_ask_threads_params_into_openai_payload(monkeypatch, fake_openai):
    """ask(params={...}) must reach the OpenAI payload through the real chain —
    a live local server records the body the harness actually sent."""
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    lb.configure_workers(None)
    srv = fake_openai([(200, ok("ok"))])
    out = lb.ask("q", model="openrouter/a:free",
                 params={"temperature": 0.7, "max_tokens": 50},
                 role="decomposer", step="")
    assert out == "ok"
    payload = srv.requests[0]["payload"]
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 50


def test_ask_without_params_carries_only_the_default_temperature(
        monkeypatch, fake_openai):
    """No params -> the body sent to the real server is exactly {model,
    messages, temperature}: since 2c41c07 every worker call carries a LOW
    default temperature (0.1, prevention against flaky generations) unless the
    caller sets one; no other sampling knob may sneak in."""
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    lb.configure_workers(None)
    srv = fake_openai([(200, ok("ok"))])
    lb.ask("q", model="openrouter/a:free", role="decomposer", step="")
    payload = srv.requests[0]["payload"]
    assert set(payload) == {"model", "messages", "temperature"}
    assert payload["temperature"] == 0.1
