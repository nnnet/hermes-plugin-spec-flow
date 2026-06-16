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
from . import config

# Transport config: NO literal defaults — the floor lives in tests/.test.env
# (config.env), and a case `workers:` block overrides it via configure_workers.
# These stay module attributes so the call path (and tests) read them live.
BACKEND = config.env("LLM_BACKEND")
BASE_URL = config.env("LLM_BASE_URL")
API_KEY = config.env("LLM_API_KEY", default="")
RETRIES = config.env("LLM_RETRIES", int)
BACKOFF = config.env("LLM_BACKOFF", float)
TIMEOUT = config.env("LLM_TIMEOUT", int)

# the OpenRouter free pool is the ONLY allowed primary for test runs
# (~1000 requests/day); paid models are forbidden unless explicitly
# unlocked. When the free pool is exhausted the ONE sanctioned fallback
# is the claude CLI on haiku.
DEFAULT_FREE_MODEL = config.env("LLM_MODEL")
ALLOW_PAID = config.env("ALLOW_PAID", bool, default=False)
FALLBACK_MODEL = config.env("FALLBACK_MODEL", default="")
FALLBACK_COOLDOWN = config.env("FALLBACK_COOLDOWN", float)

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
    global _calls_made, BACKEND, BASE_URL, API_KEY, RETRIES, BACKOFF, TIMEOUT
    global FALLBACK_MODEL, FALLBACK_COOLDOWN
    WORKERS_CFG.clear()
    WORKERS_CFG.update(cfg or {})
    # Spec-pipeline STAGES: a case may group the four stage chains under
    # ``workers.stages`` with verb names (decompose/implement/review/verify) —
    # the clean, standard-aligned shape. We flatten them onto the internal
    # role keys the engine resolves by (decomposer/implementer/reviewer/
    # verifier) via this alias map. Flat legacy keys (p4/p5) keep working
    # unchanged; an explicit flat key wins over a stage of the same meaning.
    _stages = (cfg or {}).get("stages")
    if isinstance(_stages, dict):
        _alias = {"decompose": "decomposer", "implement": "implementer",
                  "review": "reviewer", "verify": "verifier"}
        for _name, _scfg in _stages.items():
            _key = _alias.get(_name, _name)
            if _key not in WORKERS_CFG:        # don't clobber an explicit flat key
                WORKERS_CFG[_key] = _scfg
        WORKERS_CFG.pop("stages", None)
    _calls_made = 0          # a fresh case starts with a fresh budget
    # A case workers block overrides the transport floor (.test.env) ONLY for
    # keys it explicitly carries — an absent key leaves the current value
    # untouched (preserving the import-time floor and any test monkeypatch).
    c = cfg or {}
    if c.get("backend"):
        BACKEND = c["backend"]
    if c.get("base_url"):
        BASE_URL = c["base_url"]
    if "api_key" in c:
        API_KEY = c["api_key"]
    if "retries" in c:
        RETRIES = int(c["retries"])
    if "backoff" in c:
        BACKOFF = float(c["backoff"])
    if "timeout" in c:
        TIMEOUT = int(c["timeout"])
    if "fallback_model" in c:
        FALLBACK_MODEL = c["fallback_model"]
    if "fallback_cooldown_s" in c:
        FALLBACK_COOLDOWN = float(c["fallback_cooldown_s"])


class BudgetExhausted(RuntimeError):
    """The run's total LLM-call budget is spent (quota limiter)."""


# run-wide call budget — OFF by default (0). The case YAML 'workers.budget'
# wins over the env knob, consistent with the models rule. Every provider
# attempt counts (a chain that walks 3 models on quota spends 3).
_calls_made = 0
# cyclic primary rotation (#40): a monotonic call counter so successive
# calls START at different models in the chain — spreads load across the
# free pool instead of hammering chain[0] until it 429s. Opt-in via
# workers.cycle_models; off → every call starts at the primary (legacy).
_cycle_n = 0
_counters_lock = threading.Lock()


def _next_cycle() -> int:
    global _cycle_n
    with _counters_lock:
        n = _cycle_n
        _cycle_n += 1
    return n


def _cycled(chain: list, cfg: dict) -> list:
    """Rotate the chain's START position by a round-robin counter when the
    case opts into workers.cycle_models. The SET of models (and their
    relative order) is unchanged — only which one is tried first — so a
    capped primary stops being every call's first victim. Single-entry
    chains are returned as-is."""
    if not cfg.get("cycle_models") or len(chain) < 2:
        return chain
    s = _next_cycle() % len(chain)
    return chain[s:] + chain[:s]
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
               or config.env("LLM_CONCURRENCY", int))
    if want <= 0:
        return None
    if _llm_slots is None or _llm_slots_for != want:
        _llm_slots = threading.BoundedSemaphore(want)
        _llm_slots_for = want
    return _llm_slots


def _budget() -> int:
    if WORKERS_CFG.get("budget") is not None:
        return int(WORKERS_CFG["budget"])
    return config.env("LLM_BUDGET", int)


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


def chain_for(role: str, specialty: str = "") -> list[str]:
    """Ordered model chain for a role: primary first, quota fallbacks after.
    Every entry is validated against the provider registry.

    #10 (П13): when ``specialty`` is given and the role declares a chain for
    it under ``specialties.<name>``, that chain wins — a domain-specific node
    (frontend / db / api) routes to a better-fit model. Falls back to the
    role's normal chain when the specialty is unknown."""
    role_cfg = WORKERS_CFG.get(role) or {}
    defaults = WORKERS_CFG.get("defaults") or {}
    spec_chain = None
    if specialty:
        spec_cfg = (role_cfg.get("specialties") or {}).get(specialty) or {}
        spec_chain = (spec_cfg.get("models")
                      or ([spec_cfg["model"]] if spec_cfg.get("model") else None))
    chain = (spec_chain
             or role_cfg.get("models")
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


def model_for(role: str, specialty: str = "") -> str:
    """The role's primary model (head of the chain) — for logs and meta.
    #10: a specialty routes to its own chain head when configured."""
    return chain_for(role, specialty)[0]


class QuotaExhausted(RuntimeError):
    """The free pool kept returning 429 through every retry."""


def _ask_one(prompt: str, model: str, system: str | None,
             fallback: bool, timeout: int | None = None,
             params: dict | None = None) -> str:
    """Route ONE model of a chain to its provider; QuotaExhausted bubbles
    up so the caller can walk the rest of the chain.

    ``params`` is an OPEN-schema dict of sampling/generation knobs
    (temperature, max_tokens, top_p, stop, …). It is forwarded to the
    OpenAI-protocol payload as-is; the claude-CLI path has no flag for these
    fields and simply ignores them (no signature churn when new keys appear)."""
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
        # forward params only when present: a paramless call keeps the legacy
        # _ask_openai(prompt, model, system=…) signature its test stubs expect.
        if params:
            return _ask_openai(prompt, model, system=system, params=params)
        return _ask_openai(prompt, model, system=system)
    except QuotaExhausted:
        _free_down_until = time.time() + FALLBACK_COOLDOWN
        raise


# #6: the role of the call in flight, so the OpenAI path can log token usage
# tagged by role+model without threading role through every layer. Thread-local
# because the worker pool runs roles concurrently.
_call_ctx = threading.local()


_ENC = None
_ENC_TRIED = False


def _count_tokens(text: str) -> int:
    """Count tokens with a real tokenizer (tiktoken). Most free models return
    NO API ``usage`` block, so without this the economics table is near-empty;
    tiktoken's o200k_base is a close, model-agnostic counter (qwen/mimo/deepseek
    differ slightly but it is far better than a char heuristic). Falls back to
    ~chars/4 only if tiktoken is unavailable."""
    global _ENC, _ENC_TRIED
    if not text:
        return 0
    if _ENC is None and not _ENC_TRIED:
        _ENC_TRIED = True
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("o200k_base")
        except Exception:          # noqa: BLE001 — degrade, never block
            _ENC = None
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:          # noqa: BLE001
            pass
    return (len(text) + 3) // 4


def _log_token_usage(model: str, usage: dict,
                     prompt: str = "", reply: str = "") -> None:
    """#6: record token counts tagged by the in-flight role+specialist+model.
    Prefer the REAL API usage field; when the model returns none (the common
    case on the free pool) COUNT the tokens locally with a real tokenizer so
    every call still contributes. ``estimated`` marks which rows are counted
    vs reported. Best effort: never blocks the run."""
    pt = int((usage or {}).get("prompt_tokens", 0) or 0)
    ct = int((usage or {}).get("completion_tokens", 0) or 0)
    estimated = False
    if pt == 0 and ct == 0:
        if not (prompt or reply):
            return
        pt, ct, estimated = _count_tokens(prompt), _count_tokens(reply), True
        if pt == 0 and ct == 0:
            return
    try:
        from . import llm_log
        llm_log.log({"event": "token_usage",
                     "role": getattr(_call_ctx, "role", "") or "",
                     "step": getattr(_call_ctx, "step", "") or "",
                     "model": model, "prompt_tokens": pt,
                     "completion_tokens": ct, "estimated": estimated})
    except Exception:              # noqa: BLE001 — accounting never blocks work
        pass


def ask(prompt: str, *, model: str, system: str | None = None,
        fallbacks: tuple | list = (), role: str = "", step: str = "",
        params: dict | None = None) -> str:
    """Send one prompt, return the reply text.

    ``model`` + ``fallbacks`` form an ordered chain (the case YAML
    `workers:` block supplies it via chain_for); on QuotaExhausted the
    next entry answers. When the whole chain is exhausted the legacy
    terminal fallback (subscription CLI, FALLBACK_MODEL) still applies
    unless the chain already contains a 'claude/' entry.

    ``params`` (open schema: temperature/max_tokens/top_p/stop/…) rides every
    attempt in the chain and the terminal rotation, so a specialist's sampling
    config is honoured no matter which model in its chain ends up answering.

    Test: assert a fake `_ask_one`/`_ask_openai` receives the exact params dict
    passed here (see tests/workers/test_specialist_config.py)."""
    cfg = WORKERS_CFG or {}
    _call_ctx.role = role          # #6: tag token usage with the calling role
    _call_ctx.step = step          # specialist (orchestra step) for per-member split
    # cyclic primary rotation (#40): spread successive calls across the free
    # chain so one capped model isn't every call's first hit. No-op unless
    # the case sets workers.cycle_models; the model SET is unchanged.
    chain = _cycled([model, *fallbacks], cfg)
    gate = _concurrency_gate()
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
    # ENDLESS-ROTATION GUARD (#40). Two kinds of full-chain failure must be
    # treated differently:
    #   * QUOTA exhaustion — transient: the pool refills, so waiting and
    #     retrying is correct and may repeat MANY times (quota_retries). It
    #     is NOT bounded by this guard.
    #   * an ERROR round — a full pass of the chain where every model failed
    #     for a NON-quota reason (dead endpoint, malformed response, hard
    #     refusal). Waiting cannot heal this, so we never sleep on it and we
    #     cap how many CONSECUTIVE error rounds we tolerate before
    #     surrendering red. A quota round in between resets the counter (the
    #     system is alive, just throttled). Without this a persistently
    #     erroring model would spin all `rounds` (~55 min) for nothing.
    max_error_rounds = max(1, int(cfg.get("max_error_rounds", 3)))
    consecutive_error_rounds = 0
    # params ride as a kwarg ONLY when present, so a plain call (and the many
    # tests that stub _ask_one with the legacy signature) behaves byte-for-byte
    # as before. Hoisted here so both the chain loop and the terminal-fallback
    # rotation below see it.
    extra = {"params": params} if params else {}
    for attempt in range(rounds):
        if attempt:
            from . import llm_log
            if isinstance(last_exc, QuotaExhausted):
                consecutive_error_rounds = 0   # throttled, not broken — reset
                # jitter breaks the THUNDERING HERD: with concurrency>1 every
                # worker hits the 8/min wall at once, sleeps the same wait_s,
                # and wakes together to hit it again — a random spread lets
                # them retry staggered so some get through each minute
                slept = _jittered(wait_s, cfg)
                llm_log.log({"event": "quota_wait", "attempt": attempt,
                             "wait_s": round(slept, 1),
                             "detail": str(last_exc)[:160]})
                time.sleep(slept)
            else:
                # an ERROR round — don't wait; count CONSECUTIVE error rounds
                # and surrender once they reach max_error_rounds
                consecutive_error_rounds += 1
                llm_log.log({"event": "error_round", "attempt": attempt,
                             "consecutive": consecutive_error_rounds,
                             "cap": max_error_rounds,
                             "detail": str(last_exc)[:160]})
                if consecutive_error_rounds >= max_error_rounds:
                    break
        for i, m in enumerate(chain):
            _spend_call()
            try:
                if gate is not None:
                    with gate:
                        return _ask_one(prompt, m, system, fallback=i > 0,
                                        **extra)
                return _ask_one(prompt, m, system, fallback=i > 0, **extra)
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
                                    timeout=leash, **extra)
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


# OpenAI chat-completions sampling/generation fields we forward from a
# specialist's open-schema `params`. Kept as a passlist (not a hard-pinned
# signature) so a new key is enabled by one edit here, never a code change at
# the call sites — and an unknown key never reaches the wire to 400 the call.
_OPENAI_PARAM_KEYS = frozenset({
    "temperature", "max_tokens", "top_p", "top_k", "stop",
    "frequency_penalty", "presence_penalty", "seed", "response_format",
})


def _openai_params(params: dict | None) -> dict:
    """The subset of an open-schema params dict the OpenAI protocol accepts.
    Why: a specialist may carry arbitrary knobs; only the recognised sampling
    fields belong in the request body. Test: pass {temperature, bogus} and
    assert only temperature survives."""
    if not params:
        return {}
    return {k: v for k, v in params.items()
            if k in _OPENAI_PARAM_KEYS and v is not None}


def _ask_openai(prompt: str, model: str, system: str | None = None,
                params: dict | None = None) -> str:
    url = BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages, **_openai_params(params)}
    last = ""
    throttled = 0
    for attempt in range(1, RETRIES + 1):
        status, body = _http_post(url, payload, headers)
        if status == 200:
            try:
                out = json.loads(body)
                text = out["choices"][0]["message"]["content"]
                if text and text.strip():
                    _log_token_usage(model, out.get("usage") or {},
                                     prompt=prompt, reply=text)
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
