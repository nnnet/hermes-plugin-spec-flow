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
import random
import re
import subprocess
import threading
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

# per-role worker configuration — the case YAML `workers:` block.
# Shape (all lists carry parameters; models come STRICTLY from here when
# the block exists — env vars are ignored):
#   workers:
#     providers:                       # the AVAILABLE pool, with parameters
#       - {name: openrouter-free, kind: openai, model_prefix: "openrouter/",
#          require_suffix: ":free", requests_per_day: 1000}
#       - {name: claude-cli, kind: claude, model_prefix: "claude/"}
#     defaults:  {models: ["openrouter/...:free", "claude/haiku"]}
#     <role>:    {models: [...]}       # ordered chain: first is primary,
#                                      # the rest are quota fallbacks
# Backward compat: a scalar `model:` is read as a one-element chain.
WORKERS_CFG: dict = {}


def configure_workers(cfg: dict | None) -> None:
    global _calls_made
    WORKERS_CFG.clear()
    WORKERS_CFG.update(cfg or {})
    _calls_made = 0          # a fresh case starts with a fresh budget


class BudgetExhausted(RuntimeError):
    """The run's total LLM-call budget is spent (quota limiter)."""


# run-wide call budget — OFF by default (0). The case YAML 'workers.budget'
# wins over the env knob, consistent with the models rule. Every provider
# attempt counts (a chain that walks 3 models on quota spends 3).
_calls_made = 0
_counters_lock = threading.Lock()
# ONE pytest at a time: under parallel children every worker thread
# shells out into the SAME workspace — concurrent runs corrupt
# __pycache__ and sqlite fixtures. Lives here because llm_backend is the
# harness's common dependency (no import cycles).
PYTEST_LOCK = threading.Lock()

# at most workers.concurrency LLM calls in flight — the free pool's
# per-minute ceiling turns unbounded parallel calls into a 429 storm
_llm_slots: "threading.Semaphore | None" = None
_llm_slots_for = 0


def _concurrency_gate() -> "threading.Semaphore | None":
    global _llm_slots, _llm_slots_for
    want = int(WORKERS_CFG.get("concurrency")
               or os.environ.get("SPEC_FLOW_LLM_CONCURRENCY", "0"))
    if want <= 0:
        return None
    if _llm_slots is None or _llm_slots_for != want:
        _llm_slots = threading.BoundedSemaphore(want)
        _llm_slots_for = want
    return _llm_slots


def _budget() -> int:
    if WORKERS_CFG.get("budget") is not None:
        return int(WORKERS_CFG["budget"])
    return int(os.environ.get("SPEC_FLOW_LLM_BUDGET", "0"))


def calls_made() -> int:
    return _calls_made


def _spend_call() -> None:
    global _calls_made
    with _counters_lock:
        limit = _budget()
        if limit and _calls_made >= limit:
            raise BudgetExhausted(
                f"run budget of {limit} LLM calls is spent — refusing the"
                " call")
        _calls_made += 1


def _provider_for(model: str) -> dict | None:
    """Match a model against the case's provider registry (when present);
    a model outside every provider is a config error caught BEFORE any
    token is spent."""
    provs = WORKERS_CFG.get("providers") or []
    if not provs:
        return None
    for p in provs:
        if not model.startswith(p.get("model_prefix", "")):
            continue
        suffix = p.get("require_suffix")
        if suffix and suffix not in model:
            continue
        return p
    raise ValueError(
        f"model '{model}' matches no provider in the case's pool: "
        + ", ".join(str(p.get("name")) for p in provs))


def chain_for(role: str) -> list[str]:
    """Ordered model chain for a role: primary first, quota fallbacks after.
    Every entry is validated against the provider registry."""
    role_cfg = WORKERS_CFG.get(role) or {}
    defaults = WORKERS_CFG.get("defaults") or {}
    chain = (role_cfg.get("models")
             or ([role_cfg["model"]] if role_cfg.get("model") else None)
             or defaults.get("models")
             or ([defaults["model"]] if defaults.get("model") else None))
    if not chain:
        # no workers block in the case YAML — legacy env/default path
        chain = [os.environ.get(
            f"SPEC_FLOW_{role.upper().replace('-', '_')}_MODEL")
            or DEFAULT_FREE_MODEL]
    for m in chain:
        _provider_for(m)
    return list(chain)


def model_for(role: str) -> str:
    """The role's primary model (head of the chain) — for logs and meta."""
    return chain_for(role)[0]


class QuotaExhausted(RuntimeError):
    """The free pool kept returning 429 through every retry."""


def _ask_one(prompt: str, model: str, system: str | None,
             fallback: bool, timeout: int | None = None) -> str:
    """Route ONE model of a chain to its provider; QuotaExhausted bubbles
    up so the caller can walk the rest of the chain."""
    global _free_down_until
    if model.startswith("claude/"):
        # subscription-CLI provider (e.g. 'claude/haiku') — spends no API
        # budget, so the ':free' gate does not apply; 'direct' bypasses
        # the gateway so a broken proxy can't take the pool down
        cli_model = model.split("/", 1)[1]
        last_call.update(backend="claude", model=cli_model,
                         fallback=fallback)
        return _ask_claude(prompt, cli_model, system=system, direct=True,
                           timeout=timeout)
    if BACKEND != "openai":
        last_call.update(backend="claude", model=model, fallback=fallback)
        return _ask_claude(prompt, model, system=system)
    if ":free" not in model and not ALLOW_PAID:
        raise ValueError(
            f"paid model '{model}' is forbidden for test runs — only the"
            " OpenRouter ':free' pool is allowed (SPEC_FLOW_ALLOW_PAID=1"
            " to override deliberately)")
    if time.time() < _free_down_until:
        raise QuotaExhausted("free pool inside its cooldown window")
    try:
        last_call.update(backend="openai", model=model, fallback=fallback)
        return _ask_openai(prompt, model, system=system)
    except QuotaExhausted:
        _free_down_until = time.time() + FALLBACK_COOLDOWN
        raise


def ask(prompt: str, *, model: str, system: str | None = None,
        fallbacks: tuple | list = ()) -> str:
    """Send one prompt, return the reply text.

    ``model`` + ``fallbacks`` form an ordered chain (the case YAML
    `workers:` block supplies it via chain_for); on QuotaExhausted the
    next entry answers. When the whole chain is exhausted the legacy
    terminal fallback (subscription CLI, FALLBACK_MODEL) still applies
    unless the chain already contains a 'claude/' entry."""
    chain = [model, *fallbacks]
    gate = _concurrency_gate()
    cfg = WORKERS_CFG or {}
    # exhausted chain = WAIT, not death: a burst of 429s (8/min window)
    # or the nightly free-pool reset is hours away at most — a paused
    # run beats a dead one (v18 died exactly here)
    # off by default (tests, ad-hoc calls); a CASE opts in via its
    # workers block — the single source of worker behaviour
    wait_s = float(cfg.get("quota_wait_s", 300))
    rounds = 1 + int(cfg.get("quota_retries", 0))
    # terminal fallbacks form a ROTATION across providers: when one is
    # capped (claude/haiku weekly limit) the call rolls to the NEXT
    # provider instead of dying — a different provider may answer the
    # very same request. Default keeps the legacy single haiku fallback.
    rotation = _fallback_rotation(cfg, chain)
    last_exc: Exception | None = None
    for attempt in range(rounds):
        if attempt:
            from . import llm_log
            # jitter breaks the THUNDERING HERD: with concurrency>1 every
            # worker hits the 8/min wall at once, sleeps the same wait_s,
            # and wakes together to hit it again — a random spread lets
            # them retry staggered so some get through each minute
            slept = _jittered(wait_s, cfg)
            llm_log.log({"event": "quota_wait", "attempt": attempt,
                         "wait_s": round(slept, 1),
                         "detail": str(last_exc)[:160]})
            time.sleep(slept)
        for i, m in enumerate(chain):
            _spend_call()
            try:
                if gate is not None:
                    with gate:
                        return _ask_one(prompt, m, system, fallback=i > 0)
                return _ask_one(prompt, m, system, fallback=i > 0)
            except (QuotaExhausted, RuntimeError,
                    subprocess.SubprocessError) as exc:
                # the chain exists to absorb PROVIDER failure of any
                # kind — quota, throttling, a dead/hung endpoint; only
                # config errors (ValueError: paid gate, unknown
                # provider) abort the call
                last_exc = exc
        # terminal-fallback ROTATION: subscription/extra providers are a
        # last resort (free-models policy), so the first few wait-rounds
        # prefer to re-try the healthy free chain. But waiting ALL rounds
        # before touching the rotation wedged v21 for ~55 min while the
        # free pool sat in a hard cooldown — so the rotation kicks in
        # after `fallback_after_rounds` (default = last round only; a
        # case lowers it to recover from a dead pool fast). The start
        # advances each round so a capped provider isn't retried first.
        fb_after = int(cfg.get("fallback_after_rounds", rounds - 1))
        if rotation and attempt >= fb_after:
            start = attempt % len(rotation)
            order = rotation[start:] + rotation[:start]
            leash = int(cfg.get("fallback_timeout_s", 90))
            for fb in order:
                _spend_call()
                try:
                    # short leash: a hung CLI fallback must not burn the
                    # full 300s before the rotation rolls to the next one
                    return _ask_one(prompt, fb, system, fallback=True,
                                    timeout=leash)
                except (QuotaExhausted, RuntimeError,
                        subprocess.SubprocessError) as exc:
                    last_exc = exc
    raise last_exc or QuotaExhausted("no model in the chain answered")


def _fallback_rotation(cfg: dict, chain: list[str]) -> list[str]:
    """Ordered list of terminal fallback models spanning providers.
    `workers.fallback_models` is the explicit cross-provider rotation;
    legacy default is the single subscription model (FALLBACK_MODEL).
    A model already in the role chain is dropped (no point retrying it
    as a fallback)."""
    explicit = cfg.get("fallback_models")
    if explicit:
        rot = [str(m) for m in explicit]
    elif FALLBACK_MODEL:
        rot = [f"claude/{FALLBACK_MODEL}"
               if not FALLBACK_MODEL.startswith("claude/")
               else FALLBACK_MODEL]
    else:
        rot = []
    return [m for m in rot if m not in chain]


def _jittered(wait_s: float, cfg: dict) -> float:
    """wait_s plus a random spread (default ±20%) so concurrent workers
    do not wake in lockstep. Jitter 0 → deterministic (tests)."""
    frac = float(cfg.get("quota_wait_jitter", 0.2))
    if frac <= 0:
        return wait_s
    return wait_s * (1.0 + random.uniform(-frac, frac))


# ── claude CLI (fallback; gateway via ANTHROPIC_BASE_URL env) ────────────────
def _ask_claude(prompt: str, model: str, system: str | None = None,
                direct: bool = False, timeout: int | None = None) -> str:
    """``direct=True`` strips the gateway override (ANTHROPIC_BASE_URL):
    the exhaustion fallback is the SUBSCRIPTION — a broken/limited gateway
    must not take the fallback down with it (a 403 via Bifrost once killed
    a whole run while `claude` direct worked fine)."""
    last = ""
    extra = ["--append-system-prompt", system] if system else []
    env = None
    if direct:
        env = {k: v for k, v in os.environ.items()
               if k != "ANTHROPIC_BASE_URL"}
    timeout = timeout or TIMEOUT
    for _ in range(RETRIES):
        try:
            proc = subprocess.run(
                [*claude_cli.claude_cmd(), "-p", "--model", model,
                 *extra, *claude_cli.mcp_args_no_serena()],
                input=prompt, capture_output=True, text=True,
                timeout=timeout, cwd=claude_cli.agent_cwd(), env=env)
        except subprocess.TimeoutExpired:
            # a HUNG claude CLI (huge MCP system prompt load) must be a
            # retriable provider failure, NOT a fatal escape — v19 died
            # here: the CLI hung 300s while OpenRouter was healthy, and
            # TimeoutExpired slipped past the chain's (QuotaExhausted,
            # RuntimeError) net and killed the run
            last = f"timed out after {timeout}s"
            continue
        except OSError as exc:
            last = f"spawn failed: {exc}"
            continue
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
    if throttled:
        # ANY 429 among the attempts is a quota signal (the per-minute
        # ceiling of the free pool killed a whole run once: the final 429
        # surfaced as a plain RuntimeError and no fallback engaged)
        raise QuotaExhausted(
            f"free pool throttled ({throttled}/{RETRIES} tries): {last}")
    raise RuntimeError(f"openai backend failed after {RETRIES} tries: {last}")
