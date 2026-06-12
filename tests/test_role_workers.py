"""Real-worker mode, offline part.

Covers everything that must hold BEFORE a live run is worth its quota:
  * profile yaml → claude tool policy mapping (the decomposer really cannot
    Bash; only the implementer can; disabled wins over enabled),
  * the worker prompts are built from the REAL SKILL.md files,
  * the engine consultation points: reviewer verdict (REJECT bumps version
    and records a loop), researcher overrides the spike recommendation,
    verifier verdict replaces the simulated integrate PASS.

The live claude session is stubbed — live behaviour is exercised by
``run_cases.py --workers real`` (the industrial run itself).
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng         # noqa: E402
from harness import role_worker as rw         # noqa: E402

BIG = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
       "open_decisions": 0, "single_concern": False, "testable_criteria": True}
SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}

GOAL = {
    "name": "rw-goal",
    "goal": "Self-serve invoicing for freelancers",
    "target": "first invoice in < 3 min",
    "constitution": ["No card data stored."],
    "policy": {"measurable_target": True, "spend_per_action_usd": 0,
               "human_in_loop": True, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
}


def simple_decomposer(ctx):
    if ctx["depth"] == 0:
        return {"metrics": dict(BIG),
                "children": [{"id": "editor", "title": "Invoice editor"},
                             {"id": "delivery", "title": "Invoice delivery"}],
                "spike": {"question": "Reuse an OSS core?",
                          "recommendation": "decomposer's own guess"}}
    return {"metrics": dict(SMALL)}


# ── profile policy mapping ───────────────────────────────────────────────


def test_decomposer_profile_cannot_shell():
    allowed, disallowed = rw.load_profile_policy("spec-decomposer")
    assert "Bash" not in allowed          # terminal/code_execution are cut
    assert "Bash" in disallowed
    assert "Read" in allowed and "Write" in allowed   # file toolset


def test_implementer_profile_can_shell_but_not_browse():
    allowed, disallowed = rw.load_profile_policy("implementer")
    assert "Bash" in allowed
    assert "WebFetch" in disallowed       # browser disabled


def test_reviewer_profile_reads_but_cannot_delegate():
    allowed, disallowed = rw.load_profile_policy("spec-reviewer")
    assert "Read" in allowed
    assert "Task" in disallowed           # delegation disabled


def test_disabled_wins_over_enabled_overlap():
    # implementer enables code_execution AND terminal (both → Bash); if a
    # profile disabled one of them the tool must not survive in allowed
    allowed, disallowed = rw.load_profile_policy("spec-decomposer")
    assert not set(allowed) & set(disallowed)


# ── worker assembly uses the real skill text ────────────────────────────


def test_worker_session_gets_real_skill_md(monkeypatch):
    captured = {}

    def fake_run(prompt, *, system, allowed, disallowed, cwd, model):
        captured.update(system=system, allowed=allowed, prompt=prompt)
        return '{"atomic": true, "metrics": {"modules": 1, "tasks": 2, ' \
               '"interfaces": 1, "estimated_loc": 50, "open_decisions": 0, ' \
               '"single_concern": true, "testable_criteria": true}}'

    monkeypatch.setattr(rw, "_run_claude", fake_run)
    dec = rw.make_decomposer()
    out = dec({"project": {"goal": "g", "target": "t", "constitution": []},
               "node": {"id": "n1", "title": "Node"}, "parent": None,
               "depth": 1, "ancestors": ["Root"],
               "existing_nodes": [{"id": "a", "title": "A"}]})
    assert out["atomic"] is True
    # system prompt is the SKILL.md verbatim, not a paraphrase
    real_md = rw.load_skill_md("spec-flow-decompose")
    assert captured["system"] == real_md
    # tree context made it into the task prompt
    assert "a (A)" in captured["prompt"] and "Root" in captured["prompt"]


def test_leaf_depth_clamp_drops_children(monkeypatch):
    def fake_run(prompt, **kw):
        return '{"atomic": false, "metrics": {"modules": 2, "tasks": 9, ' \
               '"interfaces": 2, "estimated_loc": 400, "open_decisions": 0, ' \
               '"single_concern": false, "testable_criteria": true}, ' \
               '"children": [{"id": "x", "title": "X"}]}'
    monkeypatch.setattr(rw, "_run_claude", fake_run)
    dec = rw.make_decomposer()
    out = dec({"project": {"goal": "g", "target": "", "constitution": []},
               "node": {"id": "deep", "title": "Deep"}, "parent": "p",
               "depth": rw.LEAF_DEPTH})
    assert "children" not in out


# ── engine consultation points ───────────────────────────────────────────


def test_reviewer_reject_records_loop_and_bumps_version(plugin, tmp_path):
    rejected = []

    def reviewer(ctx):
        if ctx["node"] == "editor":
            rejected.append(ctx)
            return {"verdict": "REJECT", "reasons": ["acceptance not testable"]}
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "reviewer": reviewer})
    loops = [l for l in res.loops if l["type"] == "spec-review-reject"]
    assert loops and loops[0]["task"] == "editor"
    assert res.tasks["editor"].version >= 2          # bumped on reject
    assert rejected and rejected[0]["spec"].endswith(".md")
    verdicts = {e.verdict for e in res.events if e.gate == "spec_review"}
    assert {"PASS", "REJECT"} <= verdicts


def test_researcher_overrides_spike_recommendation(plugin, tmp_path):
    def researcher(ctx):
        assert "Reuse an OSS core?" == ctx["question"]
        return {"recommendation": "researched: build thin core", "basis": "n/a"}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "researcher": researcher})
    folded = [e for e in res.events if "folded into spec" in e.action]
    assert folded and "researched: build thin core" in folded[0].detail


def test_verifier_fail_is_recorded(plugin, tmp_path):
    def verifier(ctx):
        return {"status": "FAIL", "detail": "acceptance unmet: no e2e path"}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer,
                                  "verifier": verifier})
    fails = [l for l in res.loops if l["type"] == "integrate-fail"]
    assert fails and "acceptance unmet" in fails[0]["detail"]
    assert any(e.gate == "integrate_verify" and e.verdict == "FAIL"
               for e in res.events)


def test_without_workers_behaviour_unchanged(plugin, tmp_path):
    # no reviewer/researcher/verifier attached → the historical simulated
    # events stay exactly as before (no spec_review / integrate_verify gates)
    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": simple_decomposer})
    assert not [e for e in res.events if e.gate in ("spec_review", "integrate_verify")]
    assert not [l for l in res.loops
                if l["type"] in ("spec-review-reject", "integrate-fail")]


# ── dialogue HITL: worker questions + operator notes ─────────────────────


class _FakeChannel:
    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []
        self.replies = []
        self.note = None

    def ask(self, role, node, question):
        self.asked.append((role, node, question))
        return self.answer

    def poll_note(self, branch_capable=False):
        n, self.note = self.note, None
        return n

    def record_reply(self, role, node, position, response, note):
        self.replies.append((role, node, position, response, note))


def test_worker_question_gets_human_answer(monkeypatch):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"question": "Which currency for payouts?"}'
        assert "HUMAN ANSWER: EUR only" in prompt
        return '{"atomic": true, "metrics": {"modules": 1, "tasks": 2, ' \
               '"interfaces": 1, "estimated_loc": 40, "open_decisions": 0, ' \
               '"single_concern": true, "testable_criteria": true}}'

    monkeypatch.setattr(rw, "_run_claude", fake_run)
    chan = _FakeChannel(answer="EUR only")
    dec = rw.make_decomposer(channel=chan)
    out = dec({"project": {"goal": "g", "target": "", "constitution": []},
               "node": {"id": "pay", "title": "Payouts"}, "parent": None,
               "depth": 1})
    assert out["atomic"] is True
    assert chan.asked == [("decomposer", "pay", "Which currency for payouts?")]
    assert len(calls) == 2


def test_worker_question_without_answer_proceeds(monkeypatch):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"question": "Stripe or Adyen?"}'
        assert "Proceed on your own best" in prompt
        return '{"atomic": true, "metrics": {"modules": 1, "tasks": 1, ' \
               '"interfaces": 0, "estimated_loc": 20, "open_decisions": 0, ' \
               '"single_concern": true, "testable_criteria": true}}'

    monkeypatch.setattr(rw, "_run_claude", fake_run)
    dec = rw.make_decomposer(channel=_FakeChannel(answer=None))
    out = dec({"project": {"goal": "g", "target": "", "constitution": []},
               "node": {"id": "n", "title": "N"}, "parent": None, "depth": 1})
    assert out["atomic"] is True and len(calls) == 2


def test_operator_note_comply_recorded(monkeypatch):
    def fake_run(prompt, **kw):
        assert "OPERATOR NOTE" in prompt and "no crypto payouts" in prompt
        return '{"atomic": true, "metrics": {"modules": 1, "tasks": 1, ' \
               '"interfaces": 0, "estimated_loc": 20, "open_decisions": 0, ' \
               '"single_concern": true, "testable_criteria": true}, ' \
               '"operator_reply": {"position": "comply", ' \
               '"response": "dropping crypto rail from scope"}}'

    monkeypatch.setattr(rw, "_run_claude", fake_run)
    chan = _FakeChannel()
    chan.note = "no crypto payouts"
    dec = rw.make_decomposer(channel=chan)
    out = dec({"project": {"goal": "g", "target": "", "constitution": []},
               "node": {"id": "n", "title": "N"}, "parent": None, "depth": 1})
    assert "operator_reply" not in out          # consumed, not leaked to engine
    assert chan.replies == [("decomposer", "n", "comply",
                             "dropping crypto rail from scope",
                             "no crypto payouts")]


def test_human_channel_files(tmp_path):
    from harness import hitl as hm
    chan = hm.HumanChannel(tmp_path / "hitl")
    # operator note: written → consumed once
    chan.inbox.write_text("ship EU first", encoding="utf-8")
    assert chan.poll_note() == "ship EU first"
    assert chan.poll_note() is None
    # comply/defend audit lands in outbox
    chan.record_reply("decomposer", "n1", "defend", "EU-only cuts GMV target",
                      "ship EU first")
    out = chan.outbox.read_text(encoding="utf-8")
    assert "DEFEND" in out and "EU-only cuts GMV target" in out


# ── review policy: rework loop ───────────────────────────────────────────


def test_reject_triggers_rework_until_pass(plugin, tmp_path):
    # reviewer rejects the first version of 'editor', passes the reworked
    # one; the decomposer must receive the reviewer's reasons via ctx
    seen_feedback = []

    def decomposer(ctx):
        if ctx.get("review_feedback"):
            seen_feedback.append((ctx["node"]["id"], ctx["review_feedback"]))
        return simple_decomposer(ctx)

    state = {"editor_rejects": 0}

    def reviewer(ctx):
        if ctx["node"] == "editor" and state["editor_rejects"] == 0:
            state["editor_rejects"] += 1
            return {"verdict": "REJECT", "reasons": ["REQ-editor-1 untestable"]}
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": decomposer, "reviewer": reviewer})
    # the rework round consulted the decomposer WITH the reasons
    assert any(n == "editor" and "untestable" in fb for n, fb in seen_feedback)
    # exactly one reject episode, then PASS — the error went away
    assert sum(1 for l in res.loops if l["type"] == "spec-review-reject") == 1
    rework = [e for e in res.events if "rework after review REJECT" in e.action]
    assert rework and "attempt 1/2" in rework[0].action


def test_reject_policy_halt_stops_the_run(plugin, tmp_path):
    def reviewer(ctx):
        return {"verdict": "REJECT", "reasons": ["broken"]}
    with pytest.raises(RuntimeError, match="on_reject=halt"):
        eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                        tools=plugin.tools,
                        agents={"decomposer": simple_decomposer,
                                "reviewer": reviewer},
                        review_policy={"on_reject": "halt"})


def test_reject_policy_record_keeps_old_behaviour(plugin, tmp_path):
    calls = {"n": 0}

    def decomposer(ctx):
        calls["n"] += 1
        assert not ctx.get("review_feedback")     # no rework rounds
        return simple_decomposer(ctx)

    def reviewer(ctx):
        return {"verdict": "REJECT", "reasons": ["x"]}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": decomposer, "reviewer": reviewer},
                          review_policy={"on_reject": "record"})
    assert [l for l in res.loops if l["type"] == "spec-review-reject"]


def test_rework_closes_open_decisions_updates_header(plugin, tmp_path):
    # the reviewer demands open decisions be closed; the reworked spec closes
    # them in text — the engine must recount, sync node metrics and scrub the
    # stale 'N open decision(s)' clause from the header, or the reviewer
    # rejects the header/body contradiction until the budget burns out
    open_md = ("## Requirements\n- REQ-editor-1\n"
               "## Open decisions\n- storage: sqlite vs files\n"
               "- export format: pdf vs html\n"
               "## Acceptance criteria\n- ACR-editor-1")
    closed_md = ("## Requirements\n- REQ-editor-1\n"
                 "## Open decisions\n- None — sqlite and pdf chosen "
                 "(simplest path to the goal)\n"
                 "## Acceptance criteria\n- ACR-editor-1")

    def decomposer(ctx):
        if ctx.get("rework"):
            return {"spec_markdown": closed_md}
        if ctx["depth"] == 0:
            return {"metrics": dict(BIG),
                    "children": [{"id": "editor", "title": "Invoice editor"}]}
        return {"metrics": {**SMALL, "open_decisions": 2},
                "spec_markdown": open_md}

    state = {"rejected": False}

    def reviewer(ctx):
        if ctx["node"] == "editor" and not state["rejected"]:
            state["rejected"] = True
            return {"verdict": "REJECT",
                    "reasons": ["2 open decisions remain unresolved"]}
        return {"verdict": "PASS", "reasons": []}

    res = eng.run_project(dict(GOAL), workspace=str(tmp_path / "wk"),
                          tools=plugin.tools,
                          agents={"decomposer": decomposer, "reviewer": reviewer})
    closed = [e for e in res.events
              if "rework closed open decisions 2 → 0" in e.action]
    assert closed, "engine must record that the rework closed the decisions"
    spec = (tmp_path / "wk" / "specs" / "editor.md").read_text(encoding="utf-8")
    assert "resolve before leafing" not in spec
    assert "| open_decisions | 0 |" in spec
    assert "None — sqlite and pdf chosen" in spec


# ── free-pool backend: chat-only mode ────────────────────────────────────


def test_paid_model_is_forbidden_on_openai_backend(monkeypatch):
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "ALLOW_PAID", False)
    with pytest.raises(ValueError, match="forbidden"):
        lb.ask("hi", model="openrouter/qwen/qwen3-coder")  # no :free


def test_quota_exhaustion_falls_back_to_haiku(monkeypatch):
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "FALLBACK_MODEL", "haiku")
    calls = []

    def boom(prompt, model, system=None):
        raise lb.QuotaExhausted("429 x3")

    def claude(prompt, model, system=None, direct=False):
        calls.append(model)
        return "fallback reply"

    monkeypatch.setattr(lb, "_ask_openai", boom)
    monkeypatch.setattr(lb, "_ask_claude", claude)
    out = lb.ask("hi", model="openrouter/qwen/qwen3-coder:free")
    assert out == "fallback reply" and calls == ["haiku"]
    assert lb.last_call["fallback"] is True
    # the pool is now in cooldown: the next call skips straight to haiku
    monkeypatch.setattr(lb, "_ask_openai",
                        lambda *a, **k: pytest.fail("free pool must be skipped"))
    assert lb.ask("hi", model="openrouter/x:free") == "fallback reply"
    monkeypatch.setattr(lb, "_free_down_until", 0.0)


class _ChatWS:
    def __init__(self, root):
        self.root = str(root)

    def _write(self, rel, body, kind):
        p = pathlib.Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


GOOD_IMPL = ("def add(a, b):\n    return a + b\n")
GOOD_TEST = ("import sys\nfrom pathlib import Path\n"
             "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n"
             "from adder import add\n\n\n"
             "def test_add():\n    assert add(2, 3) == 5\n")
BAD_IMPL = ("def add(a, b):\n    return a - b\n")


def _chat_ctx(tmp_path):
    ws = _ChatWS(tmp_path)
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "adder.md").write_text(
        "## Requirements\n- REQ-adder-1 add two ints\n", encoding="utf-8")
    return {"node": "adder", "title": "Adder", "workspace": ws,
            "spec": "specs/adder.md"}


def test_chat_implementer_writes_files_and_runs_pytest(monkeypatch, tmp_path):
    import json as _json
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "ask", lambda prompt, model, system=None: _json.dumps(
        {"files": {"src/adder.py": GOOD_IMPL, "tests/test_adder.py": GOOD_TEST}}))
    impl = rw.make_implementer()
    impl(_chat_ctx(tmp_path))
    assert (tmp_path / "src" / "adder.py").exists()
    assert (tmp_path / "tests" / "test_adder.py").exists()


def test_chat_implementer_repairs_after_red_tests(monkeypatch, tmp_path):
    import json as _json
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    n = {"calls": 0}

    def fake_ask(prompt, model, system=None):
        n["calls"] += 1
        impl_body = BAD_IMPL if n["calls"] == 1 else GOOD_IMPL
        return _json.dumps({"files": {"src/adder.py": impl_body,
                                      "tests/test_adder.py": GOOD_TEST}})

    monkeypatch.setattr(lb, "ask", fake_ask)
    impl = rw.make_implementer()
    impl(_chat_ctx(tmp_path))
    assert n["calls"] == 2, "red tests must trigger exactly one repair round"
    assert "a + b" in (tmp_path / "src" / "adder.py").read_text(encoding="utf-8")


def test_chat_reviewer_inlines_spec_text(monkeypatch, tmp_path):
    import json as _json
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    (tmp_path / "specs").mkdir(parents=True)
    (tmp_path / "specs" / "n1.md").write_text("UNIQUE-SPEC-MARKER-42",
                                              encoding="utf-8")
    seen = {}

    def fake_ask(prompt, model, system=None):
        seen["prompt"] = prompt
        return _json.dumps({"verdict": "PASS", "reasons": []})

    monkeypatch.setattr(lb, "ask", fake_ask)
    rev = rw.make_reviewer()
    out = rev({"node": "n1", "spec": "specs/n1.md",
               "workspace_root": str(tmp_path), "goal": "g"})
    assert out["verdict"] == "PASS"
    assert "UNIQUE-SPEC-MARKER-42" in seen["prompt"], \
        "chat-only reviewer must receive the spec text inline"


def test_repo_map_shows_schemas_routes_signatures(tmp_path):
    from harness import repo_map
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sellers.py").write_text(
        '"""Seller onboarding feature."""\n'
        'import db\nimport registry\n'
        'db.register_schema("""\n'
        'CREATE TABLE IF NOT EXISTS sellers (id INTEGER PRIMARY KEY);\n'
        '""")\n\n'
        '@registry.route("POST", "/sellers")\n'
        'def create_seller(payload, query):\n'
        '    """Register a seller (KYC stub)."""\n'
        '    return 201, {}\n', encoding="utf-8")
    out = repo_map.build_map(str(tmp_path))
    assert "CREATE TABLE IF NOT EXISTS sellers" in out
    assert "registry.route('POST', '/sellers')" in out
    assert "def create_seller(payload, query)" in out
    assert "Seller onboarding feature" in out


def test_repo_map_excludes_platform_and_caps_budget(tmp_path):
    from harness import repo_map
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def wsgi_app(e, s):\n    pass\n",
                                             encoding="utf-8")
    (tmp_path / "src" / "big.py").write_text(
        "\n".join(f"def fn_{i}(a, b):\n    pass" for i in range(400)),
        encoding="utf-8")
    out = repo_map.build_map(str(tmp_path), exclude={"src/app.py"},
                             budget=500)
    assert "wsgi_app" not in out
    assert len(out) <= 530 and "truncated" in out


def test_extract_json_survives_fences_and_leading_braces():
    fenced = "Here it is:\n```json\n{\"a\": 1}\n```\ndone"
    assert rw._extract_json(fenced) == {"a": 1}
    tricky = "weights {0.3, 0.7} then the real one {\"b\": 2} trailing"
    assert rw._extract_json(tricky) == {"b": 2}
    with pytest.raises(ValueError):
        rw._extract_json("no json here at all")


def test_chat_implementer_survives_garbage_reply(monkeypatch, tmp_path):
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "ask", lambda prompt, model, system=None:
                        "I cannot produce JSON today {broken")
    impl = rw.make_implementer()
    assert impl(_chat_ctx(tmp_path)) is None    # surrendered, not crashed


def test_fallback_goes_direct_past_the_gateway(monkeypatch):
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(lb, "FALLBACK_MODEL", "haiku")
    monkeypatch.setattr(lb, "_ask_openai",
                        lambda *a, **k: (_ for _ in ()).throw(
                            lb.QuotaExhausted("dry")))
    seen = {}

    def fake_claude(prompt, model, system=None, direct=False):
        seen["direct"] = direct
        return "ok"

    monkeypatch.setattr(lb, "_ask_claude", fake_claude)
    assert lb.ask("hi", model="openrouter/x:free") == "ok"
    assert seen["direct"] is True, "the exhaustion fallback must bypass the gateway"
    monkeypatch.setattr(lb, "_free_down_until", 0.0)


# ── standing requirements: artifact-based late-requirement enforcement ──


def _req_channel(tmp_path):
    from harness import hitl
    ch = hitl.HumanChannel(tmp_path / "hitl")
    d = ch.requirements_dir / "web_ui"
    d.mkdir(parents=True)
    (d / "REQUIREMENT.md").write_text(
        "Minimal web interface: HTML catalog page with search.",
        encoding="utf-8")
    (d / "test_web_ui.py").write_text(
        "def test_catalog_page():\n    assert True\n", encoding="utf-8")
    return ch


def test_requirements_sync_into_smoke_and_protect(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_PROTECTED_FILES", raising=False)
    ch = _req_channel(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    synced = ch.sync_requirements(ws)
    rel = "tests/smoke/acceptance_web_ui_test_web_ui.py"
    assert synced == [rel]
    assert (ws / rel).exists()
    from harness import pytest_verifier as pv
    assert rel in pv.protected_files(), "acceptance must be worker-immutable"


def test_branch_decomposer_sees_standing_requirements(tmp_path, monkeypatch):
    import json as _json
    from harness import llm_backend as lb
    monkeypatch.setattr(lb, "BACKEND", "openai")
    ch = _req_channel(tmp_path)
    seen = {}

    def fake_ask(prompt, model, system=None):
        seen["prompt"] = prompt
        return _json.dumps({"metrics": dict(SMALL)})

    monkeypatch.setattr(lb, "ask", fake_ask)
    dec = rw.make_decomposer(workspace_dir=str(tmp_path), channel=ch)
    dec({"project": {"goal": "g", "target": "t", "constitution": []},
         "node": {"id": "L0", "title": "Root"}, "parent": None, "depth": 0,
         "ancestors": [], "parent_id": None, "existing_nodes": []})
    assert "STANDING HUMAN REQUIREMENTS" in seen["prompt"]
    assert "web_ui" in seen["prompt"]


def test_branch_addressed_note_not_burned_by_leaf(tmp_path):
    from harness import hitl
    ch = hitl.HumanChannel(tmp_path / "hitl")
    ch.inbox.write_text("@branch: add a web_ui leaf", encoding="utf-8")
    assert ch.poll_note(branch_capable=False) is None, \
        "an atomic node must not consume a @branch note"
    assert ch.inbox.read_text(encoding="utf-8").strip(), "note must survive"
    note = ch.poll_note(branch_capable=True)
    assert note and "web_ui" in note
    assert not ch.inbox.read_text(encoding="utf-8").strip()
