"""STAGE S40 (node Q6 `prompt-capture`, plan 2026-07-06T20-15).

Why: what a weak model actually implements depends on what EXACTLY reached it —
the fully assembled prompt (system + user + node context), not the prose we
think we sent. Q1 claims the leaf prompt carries a MACHINE carrier (IR), not
prose; that claim is only auditable if every real call leaves its verbatim
prompt on disk, tied to the log step. This pins observability: the single door
(``llm_backend.ask``) captures every prompt to ``<run_dir>/prompts/`` and stamps
the SAME deterministic ``call_id`` onto the ``call_start`` log event, so a prompt
file maps one-to-one to its log/trace step.

What is pinned here:
  * S40a every ``ask()`` writes ``<run_dir>/prompts/<seq>__<node>__<role>__
    <call_id>.md`` holding the full system+user prompt;
  * S40b the model's reply lands beside it (``.response.md``);
  * S40c the same ``call_id`` appears on the ``call_start`` event in the run
    log — the prompt file is unambiguously linked to the trace step.

Deterministic: the call_id is derived from a per-run counter + node + role
(never Date/random — scripts are unavailable to the engine), so the same run
produces the same file names and the same log-to-file link every time.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "harness"))

from harness_fakeapi import ok  # noqa: E402


def _run_dir(tmp_path, monkeypatch) -> pathlib.Path:
    """Wire the run log into a tmp run_dir, exactly as run_cases does
    (SPEC_FLOW_LLM_LOG = <run_dir>/llm-log.jsonl); run_dir is its parent."""
    rd = tmp_path / "run"
    rd.mkdir()
    monkeypatch.setenv("SPEC_FLOW_LLM_LOG", str(rd / "llm-log.jsonl"))
    return rd


def _prompt_files(rd: pathlib.Path) -> list[pathlib.Path]:
    pdir = rd / "prompts"
    if not pdir.exists():
        return []
    return sorted(p for p in pdir.glob("*.md")
                  if not p.name.endswith(".response.md"))


def test_every_ask_leaves_a_prompt_file(fake_openai, tmp_path, monkeypatch):
    # S40a: one live call -> one captured prompt file carrying the verbatim
    # system + user prompt.
    rd = _run_dir(tmp_path, monkeypatch)
    from harness import llm_backend as lb
    fake_openai([(200, ok("done"))])
    lb.ask("USER-PROMPT-BODY", model="openrouter/a:free", role="implementer",
           step="", system="SYSTEM-CARRIER", meta={"node": "leaf-x"})

    files = _prompt_files(rd)
    assert len(files) == 1, (
        f"one ask() must leave exactly one prompt file in {rd / 'prompts'}, "
        f"found {[p.name for p in files]}")
    body = files[0].read_text(encoding="utf-8")
    assert "USER-PROMPT-BODY" in body and "SYSTEM-CARRIER" in body, (
        "the captured prompt must hold the FULL system + user text actually "
        "sent to the model, not a summary")


def test_reply_captured_beside_prompt(fake_openai, tmp_path, monkeypatch):
    # S40b: the model's answer lands beside the prompt as <...>.response.md.
    rd = _run_dir(tmp_path, monkeypatch)
    from harness import llm_backend as lb
    fake_openai([(200, ok("MODEL-REPLY-TEXT"))])
    lb.ask("hi", model="openrouter/a:free", role="reviewer", step="",
           meta={"node": "leaf-y"})

    prompt = _prompt_files(rd)[0]
    resp = prompt.with_name(prompt.stem + ".response.md")
    assert resp.exists(), f"reply file {resp.name} must sit beside the prompt"
    assert "MODEL-REPLY-TEXT" in resp.read_text(encoding="utf-8")


def test_call_id_links_prompt_to_log_event(fake_openai, tmp_path, monkeypatch):
    # S40c: the deterministic call_id embedded in the file name also appears on
    # the call_start log event, so a prompt maps 1:1 to its trace step.
    rd = _run_dir(tmp_path, monkeypatch)
    from harness import llm_backend as lb
    fake_openai([(200, ok("done"))])
    lb.ask("hi", model="openrouter/a:free", role="implementer", step="",
           meta={"node": "leaf-z"})

    prompt = _prompt_files(rd)[0]
    # file name = <seq>__<node>__<role>__<call_id>.md — the call_id is the LAST
    # underscore-separated token before .md; it must be present on call_start.
    call_id = prompt.stem.split("__")[-1]
    assert call_id, "prompt file name must carry a non-empty call_id token"

    log = rd / "llm-log.jsonl"
    starts = [json.loads(ln) for ln in log.read_text("utf-8").splitlines()
              if ln.strip() and json.loads(ln).get("event") == "call_start"]
    assert starts, "the call must emit a call_start event"
    linked = [e for e in starts if e.get("call_id") == call_id]
    assert linked, (
        "call_start must carry the SAME call_id as the prompt file so the "
        "prompt links to the log step; call_ids on log = "
        f"{[e.get('call_id') for e in starts]}, file call_id = {call_id}")


def test_deterministic_call_id_not_random(fake_openai, tmp_path, monkeypatch):
    # the call_id must be derived from seq+node+role, not a clock/random source:
    # two calls in one run get distinct monotonic numeric seq prefixes.
    rd = _run_dir(tmp_path, monkeypatch)
    from harness import llm_backend as lb
    fake_openai([(200, ok("a")), (200, ok("b"))])
    lb.ask("p1", model="openrouter/a:free", role="implementer", step="",
           meta={"node": "n"})
    lb.ask("p2", model="openrouter/a:free", role="implementer", step="",
           meta={"node": "n"})
    files = _prompt_files(rd)
    assert len(files) == 2, "two calls -> two prompt files"
    seqs = [f.name.split("__")[0] for f in files]
    assert seqs[0] != seqs[1] and all(s.isdigit() for s in seqs), (
        "the leading token must be a monotonic numeric seq (deterministic "
        f"ordering), got {seqs}")
