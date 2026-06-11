"""Unified live-model backend — ONE switchable door for every harness agent.

Every live agent (decomposer / implementer / judge) calls ``ask(prompt, model=…)``
here instead of shelling out to a hard-coded ``claude -p``. The provider and
model are configuration, not code:

  SPEC_FLOW_LLM_BACKEND   "claude" (default) — the local claude CLI; routing to
                          a gateway happens via ANTHROPIC_BASE_URL as before.
                          "openai" — a plain OpenAI-compatible chat-completions
                          HTTP call: works against Bifrost's unified endpoint
                          (model "openrouter/qwen/qwen3-coder:free" → Bifrost
                          routes to its OpenRouter provider), OpenRouter direct,
                          or any other OpenAI-style server. No gateway config is
                          touched — the tests only choose URL + model.
  SPEC_FLOW_LLM_BASE_URL  openai backend base, default Bifrost unified
                          ("http://127.0.0.1:8080/v1"; env-overridable).
  SPEC_FLOW_LLM_API_KEY   optional Bearer for the openai backend (Bifrost local
                          needs none; OpenRouter direct does).
  SPEC_FLOW_LLM_MODEL     default model (role files may override per-role).
  SPEC_FLOW_LLM_RETRIES   attempts per call (default 3).
  SPEC_FLOW_LLM_BACKOFF   base seconds between retries (default 5); a 429
                          (free-pool throttling) waits base*attempt.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from . import claude_cli

BACKEND = os.environ.get("SPEC_FLOW_LLM_BACKEND", "claude")
BASE_URL = os.environ.get("SPEC_FLOW_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
API_KEY = os.environ.get("SPEC_FLOW_LLM_API_KEY", "")
RETRIES = int(os.environ.get("SPEC_FLOW_LLM_RETRIES", "3"))
BACKOFF = float(os.environ.get("SPEC_FLOW_LLM_BACKOFF", "5"))
TIMEOUT = int(os.environ.get("SPEC_FLOW_LLM_TIMEOUT", "300"))


def ask(prompt: str, *, model: str) -> str:
    """Send one prompt to the configured backend, return the reply text."""
    if BACKEND == "openai":
        return _ask_openai(prompt, model)
    return _ask_claude(prompt, model)


# ── claude CLI (default; gateway via ANTHROPIC_BASE_URL env) ─────────────────
def _ask_claude(prompt: str, model: str) -> str:
    last = ""
    for _ in range(RETRIES):
        proc = subprocess.run([*claude_cli.claude_cmd(), "-p", "--model", model,
                               *claude_cli.mcp_args_no_serena()],
                              input=prompt, capture_output=True, text=True,
                              timeout=TIMEOUT, cwd=claude_cli.agent_cwd())
        if proc.returncode == 0 and proc.stdout.strip():
            return claude_cli.strip_headroom_banner(proc.stdout)
        last = (proc.stderr or proc.stdout)[-300:]
    raise RuntimeError(f"claude CLI failed after {RETRIES} tries: {last}")


# ── OpenAI-compatible HTTP (Bifrost unified / OpenRouter / any) ──────────────
def _http_post(url: str, payload: dict, headers: dict) -> tuple[int, str]:
    """Tiny urllib POST; returns (status, body). Separated for test stubbing."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                       # non-2xx
        return e.code, e.read().decode("utf-8", "replace")


def _ask_openai(prompt: str, model: str) -> str:
    url = BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}]}
    last = ""
    for attempt in range(1, RETRIES + 1):
        status, body = _http_post(url, payload, headers)
        if status == 200:
            try:
                out = json.loads(body)
                text = out["choices"][0]["message"]["content"]
                if text and text.strip():
                    return text
                last = "empty completion"
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                last = f"bad response shape: {exc}: {body[-200:]}"
        else:
            last = f"HTTP {status}: {body[-200:]}"
        # free-pool throttling (429): honour the server-suggested pause when
        # present (OpenRouter sends retry_after_seconds), else back off harder
        if status == 429:
            m = re.search(r'"retry_after_seconds"\s*:\s*([0-9.]+)', body)
            time.sleep(min(90.0, float(m.group(1)) + 2) if m else BACKOFF * attempt)
        else:
            time.sleep(BACKOFF)
    raise RuntimeError(f"openai backend failed after {RETRIES} tries: {last}")
