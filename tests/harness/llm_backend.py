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

# the OpenRouter free pool is the ONLY allowed primary for test runs
# (~1000 requests/day); paid models are forbidden unless explicitly
# unlocked. When the free pool is exhausted the ONE sanctioned fallback
# is the claude CLI on haiku.
DEFAULT_FREE_MODEL = os.environ.get(
    "SPEC_FLOW_LLM_MODEL", "openrouter/qwen/qwen3-coder:free")
ALLOW_PAID = os.environ.get("SPEC_FLOW_ALLOW_PAID", "") == "1"
FALLBACK_MODEL = os.environ.get("SPEC_FLOW_FALLBACK_MODEL", "haiku")
FALLBACK_COOLDOWN = float(os.environ.get("SPEC_FLOW_FALLBACK_COOLDOWN", "600"))

# once the free pool proves exhausted, skip it for a cooldown window
# instead of burning the full retry ladder on every call
_free_down_until = 0.0
last_call: dict = {}    # {"backend":…, "model":…, "fallback":bool} — for logs


class QuotaExhausted(RuntimeError):
    """The free pool kept returning 429 through every retry."""


def ask(prompt: str, *, model: str, system: str | None = None) -> str:
    """Send one prompt to the configured backend, return the reply text.

    openai backend: free-pool-only guard + automatic one-step fallback to
    the claude CLI (FALLBACK_MODEL) when the pool is exhausted."""
    global _free_down_until
    if BACKEND != "openai":
        last_call.update(backend="claude", model=model, fallback=False)
        return _ask_claude(prompt, model, system=system)
    if ":free" not in model and not ALLOW_PAID:
        raise ValueError(
            f"paid model '{model}' is forbidden for test runs — only the"
            " OpenRouter ':free' pool is allowed (SPEC_FLOW_ALLOW_PAID=1"
            " to override deliberately)")
    if time.time() >= _free_down_until:
        try:
            last_call.update(backend="openai", model=model, fallback=False)
            return _ask_openai(prompt, model, system=system)
        except QuotaExhausted:
            _free_down_until = time.time() + FALLBACK_COOLDOWN
    if not FALLBACK_MODEL:
        raise QuotaExhausted("free pool exhausted and no fallback configured")
    last_call.update(backend="claude", model=FALLBACK_MODEL, fallback=True)
    return _ask_claude(prompt, FALLBACK_MODEL, system=system)


# ── claude CLI (fallback; gateway via ANTHROPIC_BASE_URL env) ────────────────
def _ask_claude(prompt: str, model: str, system: str | None = None) -> str:
    last = ""
    extra = ["--append-system-prompt", system] if system else []
    for _ in range(RETRIES):
        proc = subprocess.run([*claude_cli.claude_cmd(), "-p", "--model", model,
                               *extra, *claude_cli.mcp_args_no_serena()],
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


def _ask_openai(prompt: str, model: str, system: str | None = None) -> str:
    url = BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages}
    last = ""
    throttled = 0
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
            throttled += 1
            m = re.search(r'"retry_after_seconds"\s*:\s*([0-9.]+)', body)
            time.sleep(min(90.0, float(m.group(1)) + 2) if m else BACKOFF * attempt)
        else:
            time.sleep(BACKOFF)
    if throttled == RETRIES:
        raise QuotaExhausted(
            f"free pool throttled through {RETRIES} tries: {last}")
    raise RuntimeError(f"openai backend failed after {RETRIES} tries: {last}")
