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

# PROVIDER circuit breaker: the injected provider (the Bifrost chain) is tried
# FIRST on every ask(); when it is DOWN (e.g. repeated 504s from a dead upstream)
# that tax is paid on every single call and one leaf can burn 20+ min waiting on
# it. After `provider_breaker_fails` consecutive failures the breaker OPENS for
# `provider_breaker_cooldown_s`: the provider is skipped and the call goes
# straight to the working local chain. A success closes it. Model-independent —
# it keys on the provider label (provider:model), so only the dead route is cut.
_provider_breaker: dict = {}   # plabel -> {"fails": int, "open_until": float}


def _breaker_open(plabel: str) -> bool:
    st = _provider_breaker.get(plabel)
    return bool(st) and time.monotonic() < st.get("open_until", 0.0)


def _breaker_record(plabel: str, ok: bool, cfg: dict) -> None:
    if ok:
        _provider_breaker.pop(plabel, None)
        return
    need = max(1, int(cfg.get("provider_breaker_fails", 2)))
    cooldown = float(cfg.get("provider_breaker_cooldown_s", 120))
    st = _provider_breaker.setdefault(plabel, {"fails": 0, "open_until": 0.0})
    st["fails"] += 1
    if st["fails"] >= need:
        st["open_until"] = time.monotonic() + cooldown

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
    _PROVIDER_SLOTS.clear()    # rebuild per-provider gates under the new config
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
    _MODEL_5XX.clear()       # fresh per-model health for the new run
    _MODEL_429.clear()       # fresh per-model quota ledger for the new run
    _MODEL_DOWN.clear()
    _MODEL_OK.clear()        # fresh per-model success ledger for the new run
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

# Per-model run-scoped circuit breaker. A model that returns 5xx (504 gateway
# timeout / 503 unavailable) repeatedly in a run is degraded for the rest of the
# run — keep paying its full per-call timeout on every later call is the v063
# cost sink (mimo-v2.5 504'd ~121s each, on dozens of calls). After N 5xx the
# model is dropped from the chain so calls go straight to the healthy fallback.
# Reset per run in configure(). Threshold: workers.model_breaker_5xx (default 2).
_MODEL_5XX: "dict[str, int]" = {}
_MODEL_DOWN: "set[str]" = set()
# Companion breaker for QUOTA (429). Unlike a 5xx, a 429 is normally transient
# (the free pool refills), so the chain WAITS on it rather than retiring the
# model. But a DAILY-exhausted free pool 429s continuously — waiting is then
# futile and every call crawls through `quota_retries` dead rounds before the
# claude/Meridian rotation is even tried. So count 429s cumulatively: once a
# model crosses `model_breaker_429` (default 4) it is retired for the run like a
# 5xx, and EVERY role (decomposer/coder/tester/…) falls straight through to the
# healthy subscription fallback (claude via Meridian) with no further waiting.
# This is provider-health routing, case-independent — not a per-case model swap.
_MODEL_429: "dict[str, int]" = {}
# models that produced at least one real answer THIS run. A model with a track
# record is the workhorse — a single transient timeout under load must not
# retire it for the rest of the run (live v096: claude/sonnet timed out once at
# 75s and was retired @1, forcing every later call onto a weak model -> 40 min +
# weak_implementer). retire@1-on-timeout therefore applies ONLY to a model that
# never worked this run (dead from the start, like a 401'ing provider).
_MODEL_OK: "set[str]" = set()


def _retire_after_one(reason: str, model: str) -> bool:
    """A failure that will not heal this run -> retire the model after a SINGLE
    occurrence. AUTH (401/403, a stale key) never heals. A TIMEOUT is fatal @1
    only for a model that has not produced one answer this run (a provider dead
    from the start); a PROVEN model's timeout is a transient blip under load and
    waits the configured threshold instead (live v096 regression guard)."""
    return reason == "auth" or (reason == "timeout" and model not in _MODEL_OK)


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

# ONE universal concurrency gate, keyed by PROVIDER (the model id's first
# segment). A provider's limit is declared in its config and handles both reasons
# a call must queue:
#   * a FREE pool (openrouter/xiaomimimo) has a per-minute ceiling — unbounded
#     parallel calls become a 429 storm;
#   * a SUBSCRIPTION provider (claude over Bifrost -> Meridian -> Max) is
#     rate-limited, so concurrent calls throttle each other into timeouts (live
#     v097: 3 parallel claude calls each ballooned past the 75s cap, 0/9 ok).
# Every call — whatever the backend (HTTP or the legacy CLI) — queues on the same
# per-provider semaphore: no traffic-path split, no per-model special-case, no
# duplicate gate. Concurrency comes from workers.provider_concurrency[<provider>]
# (the harness mirror of a Bifrost provider's `concurrency`), falling back to the
# shared max_concurrent_llm_requests default. Set a provider to 1 to serialise it
# (claude), 0 to leave it unbounded.
_PROVIDER_SLOTS: "dict[str, tuple]" = {}


def _provider_of(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower() or "?"


def _provider_gate(model: str) -> "threading.Semaphore | None":
    prov = _provider_of(model)
    pc = WORKERS_CFG.get("provider_concurrency") or {}
    # the old `concurrency` key is still read so resuming a pre-rename run works.
    default = int(WORKERS_CFG.get("max_concurrent_llm_requests")
                  or WORKERS_CFG.get("concurrency")
                  or config.env("LLM_CONCURRENCY", int))
    want = int(pc.get(prov, pc.get("default", default)))
    if want <= 0:
        return None
    sem, sem_for = _PROVIDER_SLOTS.get(prov, (None, None))
    if sem is None or sem_for != want:
        sem = threading.BoundedSemaphore(want)
        _PROVIDER_SLOTS[prov] = (sem, want)
    return sem


def _provider_timeout(model: str) -> int:
    """Per-provider per-call wall cap, declared in config alongside the gate
    (workers.provider_timeout[<provider>]). One global timeout cannot fit a
    mixed pool: a flaky free model must fail fast (~75s) so a dead endpoint is
    dropped quickly, but the capable workhorse legitimately needs longer for a
    BIG codegen prompt over a multi-hop subscription path (live v098: claude
    prompts ran 9k-18k chars and a real generation took ~30s for 4k chars, so a
    18k-char call needs well over 75s — at 75s every claude codegen timed out,
    0/24, and the run fell onto weak models -> weak_implementer). Falls back to
    the shared LLM_TIMEOUT default."""
    pt = WORKERS_CFG.get("provider_timeout") or {}
    return int(pt.get(_provider_of(model), pt.get("default", TIMEOUT)))


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


def assert_provider_chain(*, smoke: bool = True) -> None:
    """Preflight invariant (ARCHITECTURE RULE): every LLM provider the run can
    call MUST route through Bifrost; claude additionally through the Bifrost
    anthropic provider -> Meridian -> subscription, NEVER a direct `claude -p`
    CLI. Raises RuntimeError (the runner turns it into a hard refusal) if any
    model bypasses Bifrost, or if the chain is configured but not LIVE — the
    v074/v075 trap was a healthy-looking Bifrost in front of a DEAD Meridian, so
    config alone is not enough: we smoke one claude call end-to-end."""
    import urllib.parse as _up

    def _netloc(url: str) -> str:
        try:
            return _up.urlsplit(url if "://" in url else "http://" + url).netloc
        except Exception:
            return ""

    cfg = WORKERS_CFG or {}
    bif = (cfg.get("base_url") or BASE_URL or "").rstrip("/")
    if not bif:
        raise RuntimeError("provider-chain preflight: workers.base_url (Bifrost) is unset")
    bif_host = _netloc(bif)
    gw = cfg.get("claude_gateway") if isinstance(cfg.get("claude_gateway"), dict) else None
    gw_url = ((gw or {}).get("base_url") or "").rstrip("/")
    model_map = (gw or {}).get("model_map") or {}

    # 1) every model id the run can call (tiers + flattened stages + defaults +
    #    fallback + implement-team specialists that are raw models)
    models: set = set()
    for tier in (cfg.get("tiers") or {}).values():
        models.update((tier or {}).get("models") or [])
    for key in ("decomposer", "implementer", "reviewer", "verifier", "defaults"):
        models.update(((cfg.get(key) or {}).get("models")) or [])
    models.update(cfg.get("fallback_models") or [])
    for sp in (((cfg.get("implementer") or {}).get("team") or {}).get("specialists") or []):
        # agent-platform seams (provider: hermes / mission-control) call their OWN
        # llm internally — not a direct Bifrost model in THIS harness; skip them.
        if sp.get("provider") in ("hermes", "mission-control"):
            continue
        if sp.get("model"):
            models.add(sp["model"])

    # 2) claude must ride the Bifrost gateway, never the direct CLI
    claude_models = sorted(m for m in models if str(m).startswith("claude/"))
    if claude_models:
        if not gw_url:
            raise RuntimeError(
                "provider-chain preflight: claude models would use the direct CLI "
                f"(no workers.claude_gateway). Route them via Bifrost {bif}. "
                f"Offending: {claude_models}")
        if _netloc(gw_url) != bif_host:
            raise RuntimeError(
                f"provider-chain preflight: claude_gateway {gw_url} is not Bifrost ({bif})")
        for m in claude_models:
            short = m.split("/", 1)[1] if "/" in m else m
            if m not in model_map and short not in model_map:
                raise RuntimeError(
                    f"provider-chain preflight: claude model '{m}' has no "
                    "claude_gateway.model_map entry; Bifrost cannot resolve it")

    # 3) a provider that overrode base_url to a non-Bifrost host is a bypass
    for prov in (cfg.get("providers") or []):
        pburl = prov.get("base_url")
        if pburl and _netloc(pburl) != bif_host:
            raise RuntimeError(
                f"provider-chain preflight: provider '{prov.get('name')}' base_url "
                f"{pburl} bypasses Bifrost ({bif})")

    # 4) the chain must be LIVE — smoke one claude call all the way through
    if smoke and claude_models:
        m = claude_models[0]
        short = m.split("/", 1)[1] if "/" in m else m
        target = model_map.get(m) or model_map.get(short)
        data = json.dumps({"model": target, "max_tokens": 5,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        try:
            req = urllib.request.Request(
                gw_url + "/chat/completions", data=data,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"provider-chain preflight: claude smoke through Bifrost failed "
                f"({gw_url} -> {target}): {exc}. Is Meridian (:3456) up?") from exc
        if isinstance(payload, dict) and (payload.get("error") or not payload.get("choices")):
            raise RuntimeError(
                f"provider-chain preflight: Bifrost gave no completion for {target}: "
                f"{str(payload)[:200]} — chain (Bifrost->Meridian->sub) is broken")

    print(f"[preflight] provider-chain OK — all models via Bifrost {bif}"
          + (f"; claude->gateway->Meridian (smoked {claude_models[0]})"
             if (smoke and claude_models) else ""))


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


def _is_free_model(model: str) -> bool:
    """A model is FREE if its id carries the OpenRouter ':free' marker OR it
    routes to a provider the case declared with a free daily quota
    (``requests_per_day``). The latter covers providers like ``xiaomimimo``
    whose account keys are free even though the model id has no ':free' suffix —
    so 'xiaomimimo/mimo-v2.5' must NOT be rejected by the paid-model guard."""
    if ":free" in model:
        return True
    try:
        prov = _provider_for(model)
    except ValueError:
        return False
    if not prov:
        return False
    # A LOCAL self-hosted provider (e.g. LMStudio) is free by nature: it has no
    # daily quota to declare, so `requests_per_day` is absent — but it is NOT a
    # paid API. The paid-model guard exists to block accidental paid OpenRouter
    # models, not an operator-declared local backend. Honor an explicit
    # `local: true` marker on the provider.
    if prov.get("local"):
        return True
    return bool(prov.get("requests_per_day"))


def chain_for(role: str, specialty: str = "", tier: str = "") -> list[str]:
    """Ordered model chain for a role: primary first, quota fallbacks after.
    Every entry is validated against the provider registry.

    #10 (П13): when ``specialty`` is given and the role declares a chain for
    it under ``specialties.<name>``, that chain wins — a domain-specific node
    (frontend / db / api) routes to a better-fit model. Falls back to the
    role's normal chain when the specialty is unknown.

    424 (complexity routing): when ``tier`` names a KNOWN power tier
    (workers.tiers.<tier>), that chain wins over everything — the engine sets
    it at spawn from the node's leaf_check metrics, so a hard node routes to
    the strong chain and a trivial one to the cheap chain. An unknown/unset
    tier is ignored (the normal role/specialty resolution applies)."""
    role_cfg = WORKERS_CFG.get(role) or {}
    defaults = WORKERS_CFG.get("defaults") or {}
    tier_chain = None
    if tier:
        tier_chain = ((WORKERS_CFG.get("tiers") or {}).get(tier) or {}).get("models")
    spec_chain = None
    if specialty:
        spec_cfg = (role_cfg.get("specialties") or {}).get(specialty) or {}
        spec_chain = (spec_cfg.get("models")
                      or ([spec_cfg["model"]] if spec_cfg.get("model") else None))
    # tier fallback: a role/defaults may name a power TIER instead of listing
    # models, so the doctor's escalate_tier and the complexity->tier router share
    # ONE place that defines what "strong"/"weak" means (workers.tiers).
    def _tier_models(cfg: dict):
        t = cfg.get("tier")
        if not t:
            return None
        return ((WORKERS_CFG.get("tiers") or {}).get(t) or {}).get("models")
    chain = (list(tier_chain) if tier_chain
             else spec_chain
             or role_cfg.get("models")
             or ([role_cfg["model"]] if role_cfg.get("model") else None)
             or _tier_models(role_cfg)
             or defaults.get("models")
             or ([defaults["model"]] if defaults.get("model") else None)
             or _tier_models(defaults))
    if not chain:
        # no workers block in the case YAML — legacy env/default path
        chain = [os.environ.get(
            f"SPEC_FLOW_{role.upper().replace('-', '_')}_MODEL")
            or DEFAULT_FREE_MODEL]
    for m in chain:
        _provider_for(m)
    return list(chain)


def model_for(role: str, specialty: str = "", tier: str = "") -> str:
    """The role's primary model (head of the chain) — for logs and meta.
    #10: a specialty routes to its own chain head when configured.
    424: a complexity tier routes to its tier chain head when set."""
    return chain_for(role, specialty, tier)[0]


def chain_for_tier(tier: str) -> list[str]:
    """Resolve a power TIER name (weak/medium/strong) to its model chain.

    The single place that turns a tier into models — the doctor's escalate_tier
    and the complexity->tier router both go through here, so swapping a tier's
    model is one edit in workers.tiers. Falls back to the default role chain when
    the tier is unknown/unset."""
    models = ((WORKERS_CFG.get("tiers") or {}).get(tier) or {}).get("models")
    return list(models) if models else chain_for("defaults")


def _with_default_params(params: dict | None, cfg: dict | None = None,
                         role: str = "") -> dict:
    """Prevention: every worker call gets a LOW default temperature unless the
    caller set one explicitly (deterministic output, fewer flaky generations).
    Default from workers.temperature or SPEC_FLOW_WORKER_TEMPERATURE (floor 0.1).

    Layering (weakest first): default temperature < STAGE-level params
    (``workers.stages.<stage>.params`` — flattened onto the role key by
    configure_workers, so a case can e.g. turn reasoning off for review/verify)
    < explicit caller params (a team specialist's own config always wins).
    Test: configure {reviewer: {params: {reasoning: "off"}}} and assert an
    ask(role='reviewer') call carries reasoning="off" in its params."""
    cfg = cfg if cfg is not None else (WORKERS_CFG or {})
    try:
        default_t = float(cfg.get("temperature",
                                  os.environ.get("SPEC_FLOW_WORKER_TEMPERATURE",
                                                 "0.1") or 0.1))
    except (ValueError, TypeError):
        default_t = 0.1
    merged = {"temperature": default_t}
    if role:
        role_cfg = cfg.get(role)
        stage_params = role_cfg.get("params") if isinstance(role_cfg, dict) else None
        if isinstance(stage_params, dict):
            merged.update(stage_params)
    merged.update(params or {})
    return merged


class QuotaExhausted(RuntimeError):
    """The free pool kept returning 429 through every retry."""


def _ask_one(prompt: str, model: str, system: str | None,
             fallback: bool, timeout: int | None = None,
             params: dict | None = None, tools: dict | None = None,
             cwd: str | None = None) -> str:
    """Route ONE model of a chain to its provider; QuotaExhausted bubbles
    up so the caller can walk the rest of the chain.

    ``params`` is an OPEN-schema dict of sampling/generation knobs
    (temperature, max_tokens, top_p, stop, …). It is forwarded to the
    OpenAI-protocol payload as-is; the claude-CLI path has no flag for these
    fields and simply ignores them (no signature churn when new keys appear)."""
    global _free_down_until
    if model.startswith("claude/"):
        # subscription provider (e.g. 'claude/haiku') — spends no API budget, so
        # the ':free' gate does not apply.
        cli_model = model.split("/", 1)[1]
        gw = _claude_gateway()
        if gw:
            # claude routed through a configured OpenAI-compatible GATEWAY
            # (e.g. bifrost serving anthropic/claude-haiku-4-5): it joins the
            # SAME real HTTP path as every other model, so the fallback arm is
            # deterministically exercisable (a real 429/200) without a CLI. The
            # model_map renames the short id to the gateway's id when given.
            mapped = (gw.get("model_map") or {}).get(cli_model, cli_model)
            last_call.update(backend="claude-http", model=mapped,
                             fallback=fallback)
            return _ask_openai(prompt, mapped, system=system, params=params,
                               base_url=gw["base_url"],
                               api_key=gw.get("api_key"))
        # default: the subscription CLI. 'direct' bypasses the gateway override
        # so a broken proxy can't take the fallback down. tools/cwd carry the
        # agentic worker policy through to the CLI session.
        last_call.update(backend="claude", model=cli_model,
                         fallback=fallback)
        return _ask_claude(prompt, cli_model, system=system, direct=True,
                           timeout=timeout, allowed=(tools or {}).get("allowed"),
                           disallowed=(tools or {}).get("disallowed"), cwd=cwd,
                           log_model=model)
    if BACKEND != "openai":
        # the agentic claude backend (non-chat workers run the CLI with tools).
        # When a gateway is configured it joins the SAME real HTTP path — so an
        # agentic decomposer's assembled system+prompt are exercisable over a
        # real socket without spawning a CLI; otherwise the subscription CLI runs
        # the tool-enabled session.
        gw = _claude_gateway()
        if gw:
            mapped = (gw.get("model_map") or {}).get(model, model)
            last_call.update(backend="claude-http", model=mapped,
                             fallback=fallback)
            return _ask_openai(prompt, mapped, system=system, params=params,
                               base_url=gw["base_url"], api_key=gw.get("api_key"))
        last_call.update(backend="claude", model=model, fallback=fallback)
        return _ask_claude(prompt, model, system=system, timeout=timeout,
                           allowed=(tools or {}).get("allowed"),
                           disallowed=(tools or {}).get("disallowed"), cwd=cwd)
    if not _is_free_model(model) and not ALLOW_PAID:
        raise ValueError(
            f"paid model '{model}' is forbidden for test runs — only free"
            " models are allowed: an OpenRouter ':free' id, or one routed to a"
            " provider declared with a free daily quota (requests_per_day),"
            " e.g. xiaomimimo. (SPEC_FLOW_ALLOW_PAID=1 to override)")
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


def ask(prompt: str, *, model: str, role: str, step: str,
        system: str | None = None, fallbacks: tuple | list = (),
        params: dict | None = None, meta: dict | None = None,
        provider: str = "", provider_call=None,
        tools: dict | None = None, cwd: str | None = None) -> str:
    """Send one prompt, return the reply text.

    ``role`` (the pipeline STAGE: decomposer/implementer/reviewer/verifier) and
    ``step`` (the orchestra SPECIALIST, or "" when the call is not a team step)
    are MANDATORY keyword arguments — a call that omits them is a bug and raises,
    so every LLM call is attributable to a stage+specialist (no empty rows in the
    token/idle accounting). Pass ``step=""`` explicitly for a solo (non-team)
    call to acknowledge there is no specialist.

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
    if not role:
        raise ValueError(
            "ask() requires a non-empty `role` (the pipeline stage: decomposer/"
            "implementer/reviewer/verifier). Every LLM call must be attributable "
            "to a stage — a missing/empty role hides token and idle accounting.")
    cfg = WORKERS_CFG or {}
    # low default temp for all workers + stage-level params (workers.stages.
    # <stage>.params) under the caller's explicit params
    params = _with_default_params(params, cfg, role=role)
    _call_ctx.role = role          # #6: tag token usage with the calling role
    _call_ctx.step = step          # specialist (orchestra step) for per-member split
    _call_ctx.last_usage = None    # cleared each call; _ask_openai stashes real usage here

    # SINGLE-DOOR call-boundary logging (folded in from the old llm_log.timed_ask
    # wrapper): every ask() — whatever backend or chain it ends up using — emits
    # exactly one call_start now and one call_ok / call_error at its exit, so no
    # caller needs its own wrapper and there is ONE place all LLM logging lives.
    # `meta` is the open-schema descriptive context (node/depth/purpose/mode/…)
    # the callers used to pass to timed_ask.
    from . import llm_log as _llm_log
    _bctx = {k: v for k, v in (meta or {}).items()
             if k not in ("prompt", "system", "text", "reply")}
    _bctx.setdefault("role", role)
    _bctx.setdefault("model", model)
    if step:
        _bctx.setdefault("step", step)
    _b_t0 = time.monotonic()
    _llm_log.log({"event": "call_start", **_bctx, "prompt_chars": len(prompt)})
    # the outcome echoes the identity keys but NOT `model`, so a start/result
    # pair counts the request exactly once (model lives on call_start).
    _bident = {k: _bctx[k] for k in ("role", "node", "depth", "purpose", "mode",
                                     "step") if k in _bctx}

    def _ret(reply: str, used_model: str) -> str:
        # UNIVERSAL token accounting at ask()'s SINGLE exit point: use the
        # model's real usage when a backend captured one (stashed in
        # _call_ctx.last_usage), else count prompt+reply with tiktoken; then log
        # the token_usage event and return. Every successful ask() — openai,
        # claude, any backend — therefore produces exactly one token_usage row.
        usage = getattr(_call_ctx, "last_usage", None) or {}
        _call_ctx.last_usage = None
        _log_token_usage(used_model, usage, prompt=prompt, reply=reply)
        _llm_log.log({"event": "call_ok", **_bident,
                      "latency_s": round(time.monotonic() - _b_t0, 2),
                      "reply_chars": len(reply)})
        return reply
    # cyclic primary rotation (#40): spread successive calls across the free
    # chain so one capped model isn't every call's first hit. No-op unless
    # the case sets workers.cycle_models; the model SET is unchanged.
    chain = _cycled([model, *fallbacks], cfg)
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
    if tools:   # agentic claude worker tool policy → threaded to _ask_one
        extra["tools"] = tools
    if cwd:
        extra["cwd"] = cwd
    # PROVIDER folded INTO the single door (Phase 2): when a remote specialist
    # supplies an adapter call, it is the FIRST link of the chain — tried before
    # any local model. If the remote is unreachable or its contract differs, the
    # door logs the degradation as a normal llm_fallback hop and falls through to
    # the local model chain below. This replaces the old _call_model bypass (its
    # private try/except + provider_fallback event), so a provider attempt and
    # its degradation now live in ask()'s ONE logging path like every other hop.
    if provider_call is not None:
        _plabel = f"{provider or 'provider'}:{model}"
        if _breaker_open(_plabel):
            # the provider tripped its breaker on recent failures — skip the
            # dead route entirely (no wait) and go straight to the local chain
            _log_event({"event": "llm_fallback", "from_model": _plabel,
                        "to_model": chain[0] if chain else None,
                        "provider": provider,
                        "reason": "provider circuit open (skipped)",
                        "terminal": False})
        else:
            _spend_call()
            try:
                _raw = provider_call(prompt)
            except Exception as exc:  # noqa: BLE001 — remote down → local chain
                last_exc = exc
                _breaker_record(_plabel, False, cfg)
                _log_event({"event": "llm_fallback", "from_model": _plabel,
                            "to_model": chain[0] if chain else None,
                            "provider": provider,
                            "reason": _failure_reason(exc), "terminal": False})
            else:
                _breaker_record(_plabel, True, cfg)
                return _ret(_raw, _plabel)
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
        # drop models the run already saw fail repeatedly — they cost a full
        # timeout per call for nothing. When the WHOLE chain is retired, do NOT
        # blindly re-try the dead chain[-1] (v133: a one-model weak tier paid
        # mimo's 75s timeout 28× AFTER it was retired — the only healthy
        # fallback claude/sonnet had itself tripped on 2 transient 5xx, so
        # `or chain[-1:]` forced the dead mimo back every call, ~35 min wasted).
        # Instead revive the LEAST-FAILED known model, preferring the
        # cross-provider rotation (fallback_models) and breaking ties by fewest
        # recorded failures, so a flaky single-model tier rolls to the healthiest
        # survivor instead of a guaranteed timeout. Model-independent (no names).
        live = [m for m in chain if m not in _MODEL_DOWN]
        if not live:
            # every chain model is retired -> route to the least-failed KNOWN
            # model, PREFERRING the rotation (claude via Meridian, which carries
            # no free daily quota). Exclude retired models where possible and
            # rank by TOTAL failures (5xx + 429) so an exhausted free pool never
            # wins the tie back (a 429-retired model has 5xx=0 and would
            # otherwise sort first). claude stays the guaranteed last resort.
            _cand = [m for m in (list(rotation) + list(chain))
                     if m not in _MODEL_DOWN]
            _cand = list(dict.fromkeys(_cand))
            _cand.sort(key=lambda _m: _MODEL_5XX.get(_m, 0) + _MODEL_429.get(_m, 0))
            live = _cand[:1] or (list(rotation)[:1] or chain[-1:])
        _brk = int((cfg or {}).get("model_breaker_5xx", 2))
        for i, m in enumerate(live):
            _spend_call()
            # ONE per-provider gate: claude serialises (subscription rate),
            # the free pool caps at its 429 ceiling — same mechanism, declared
            # per provider (live v097: 3 parallel claude calls self-throttled
            # past the 75s timeout).
            eff_gate = _provider_gate(m)
            try:
                if eff_gate is not None:
                    with eff_gate:
                        _raw = _ask_one(prompt, m, system, fallback=i > 0,
                                        **extra)
                else:
                    _raw = _ask_one(prompt, m, system, fallback=i > 0, **extra)
                _MODEL_OK.add(m)        # produced a real answer -> a workhorse
                return _ret(_raw, m)
            except (QuotaExhausted, RuntimeError,
                    subprocess.SubprocessError) as exc:
                # the chain exists to absorb PROVIDER failure of any
                # kind — quota, throttling, a dead/hung endpoint; only
                # config errors (ValueError: paid gate, unknown
                # provider) abort the call
                last_exc = exc
                # per-model breaker: count 5xx (gateway down) and retire the
                # model for the rest of the run once it crosses the threshold,
                # so later calls skip it instead of paying its timeout again
                _reason = _failure_reason(exc)
                if _reason in ("5xx", "timeout", "auth"):
                    _MODEL_5XX[m] = _MODEL_5XX.get(m, 0) + 1
                    # An AUTH failure (401/403) is a stale/missing gateway key: it
                    # NEVER heals mid-run, so retire after ONE (live v094:
                    # xiaomimimo 401'd 16x, each an instant but wasted fallback).
                    # A TIMEOUT is retired @1 ONLY for a model that has NEVER
                    # answered this run — a provider dead from the start (e.g. a
                    # gateway that hangs every call). A model WITH a track record
                    # (_MODEL_OK) is the workhorse: a single transient timeout
                    # under load must not retire it for the rest of the run (live
                    # v096: claude/sonnet timed out once at 75s and was retired,
                    # forcing every later call onto a weak model -> 40 min +
                    # weak_implementer). A proven model's timeout, like a discrete
                    # 5xx, waits the configured threshold instead.
                    _retire_at = 1 if _retire_after_one(_reason, m) else _brk
                    if _MODEL_5XX[m] >= _retire_at and m not in _MODEL_DOWN:
                        _MODEL_DOWN.add(m)
                        _log_event({"event": "model_circuit_open", "model": m,
                                    "reason": _reason, "count": _MODEL_5XX[m]})
                elif _reason == "429" or isinstance(exc, QuotaExhausted):
                    # QUOTA breaker: a free pool that keeps 429ing is exhausted
                    # for the day. Waiting on it is futile — count cumulatively
                    # and retire it for the run once it crosses model_breaker_429,
                    # so every role (decomposer/coder/tester/…) falls straight
                    # through to the healthy subscription fallback (claude via
                    # Meridian) instead of crawling through dead quota rounds.
                    _MODEL_429[m] = _MODEL_429.get(m, 0) + 1
                    _q_at = int((cfg or {}).get("model_breaker_429", 4))
                    if _MODEL_429[m] >= _q_at and m not in _MODEL_DOWN:
                        _MODEL_DOWN.add(m)
                        _log_event({"event": "model_circuit_open", "model": m,
                                    "reason": "429", "count": _MODEL_429[m]})
                # record the fallback hop so the dashboard shows which model
                # failed (and why) and what was tried next
                _nxt = live[i + 1] if i + 1 < len(live) else (
                    rotation[0] if rotation else None)
                _log_event({"event": "llm_fallback", "from_model": m,
                            "to_model": _nxt, "reason": _failure_reason(exc),
                            "terminal": False})
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
            for j, fb in enumerate(order):
                _spend_call()
                try:
                    # short leash: a hung CLI fallback must not burn the
                    # full 300s before the rotation rolls to the next one
                    return _ret(_ask_one(prompt, fb, system, fallback=True,
                                         timeout=leash, **extra), fb)
                except (QuotaExhausted, RuntimeError,
                        subprocess.SubprocessError) as exc:
                    last_exc = exc
                    _nxt = order[j + 1] if j + 1 < len(order) else None
                    _log_event({"event": "llm_fallback", "from_model": fb,
                                "to_model": _nxt,
                                "reason": _failure_reason(exc),
                                "terminal": False})
    # the whole chain + rotation gave up — a terminal failure for the call
    _log_event({"event": "llm_fallback",
                "from_model": (chain[-1] if chain else model),
                "to_model": None,
                "reason": _failure_reason(last_exc) if last_exc else "error",
                "terminal": True})
    _llm_log.log({"event": "call_error", **_bident,
                  "latency_s": round(time.monotonic() - _b_t0, 2),
                  "error": (repr(last_exc)[:300] if last_exc
                            else "no model in the chain answered")})
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
    elif explicit is not None:
        # the case DECLARED an empty rotation — honour it literally; the
        # legacy claude default below is only for configs that say nothing
        # (an empty list silently swapped to claude made a "no fallbacks"
        # config lie, and unit tests burned real subscription calls)
        rot = []
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


def _claude_gateway() -> dict | None:
    """Config for routing the claude subscription model through an OpenAI-
    compatible HTTP gateway instead of the local CLI. Returns a dict
    ``{base_url, api_key?, model_map?}`` or None (the default → CLI).

    Source: the case `workers.claude_gateway` block, or the env
    SPEC_FLOW_CLAUDE_GATEWAY (base_url only). Why: a gateway like bifrost serves
    anthropic models over the same HTTP path as every other model, so the claude
    fallback arm shares ONE transport and is deterministically testable — while
    production keeps the subscription CLI by leaving this unset."""
    gw = (WORKERS_CFG or {}).get("claude_gateway")
    if isinstance(gw, dict) and gw.get("base_url"):
        return gw
    env_url = os.environ.get("SPEC_FLOW_CLAUDE_GATEWAY")
    return {"base_url": env_url} if env_url else None


# ── claude CLI (fallback; gateway via ANTHROPIC_BASE_URL env) ────────────────
def _ask_claude(prompt: str, model: str, system: str | None = None,
                direct: bool = False, timeout: int | None = None,
                allowed: list[str] | None = None,
                disallowed: list[str] | None = None,
                cwd: str | None = None, log_model: str | None = None) -> str:
    """The claude CLI backend. ``direct=True`` strips the gateway override
    (ANTHROPIC_BASE_URL): the exhaustion fallback is the SUBSCRIPTION — a
    broken/limited gateway must not take the fallback down with it (a 403 via
    Bifrost once killed a whole run while `claude` direct worked fine).

    ``allowed``/``disallowed``/``cwd`` turn this into the AGENTIC worker session
    (Phase 4): the tool policy becomes --allowedTools/--disallowedTools and the
    session runs in the worker's workspace, so the non-chat path is ONE backend
    under the single door (formerly role_worker._run_claude)."""
    last = ""
    extra = ["--append-system-prompt", system] if system else []
    if allowed:
        extra += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        extra += ["--disallowedTools", ",".join(disallowed)]
    env = None
    if direct:
        env = {k: v for k, v in os.environ.items()
               if k != "ANTHROPIC_BASE_URL"}
    timeout = timeout or TIMEOUT

    def _attempt(n: int, t0: float, status, abnormal: bool, error: str = "") -> None:
        # mirror _ask_openai: EVERY claude-CLI attempt is logged so the
        # dashboard sees all delay/failure causes on the subscription path too.
        # log the CANONICAL ask-level id (e.g. 'claude/haiku') so every event
        # type (llm_attempt / token_usage / outcome) keys the model identically;
        # the CLI still runs the short id in `model`.
        ev = {"event": "llm_attempt", "backend": "claude", "provider": "claude",
              "model": log_model or model, "attempt": n, "status": status,
              "latency_s": round(time.monotonic() - t0, 2), "abnormal": abnormal}
        if error:
            ev["error"] = error
        _log_event(ev)

    # MCP servers are loaded ONLY for the agentic worker session (tools given via
    # allowed/disallowed). A plain chat call (decompose/review/spec) needs no
    # tools, and booting all MCP servers per call is the killer: alone it is ~5s,
    # but under the run's concurrency many claude CLIs each spawn the full MCP set
    # at once, ballooning every call to 40-300s (and hitting the 300s timeout x
    # RETRIES). Skip MCP for chat → the claude fallback stays ~5s.
    mcp = claude_cli.mcp_args_no_serena() if (allowed or disallowed) else []
    for attempt in range(1, RETRIES + 1):
        # concurrency is bounded once, upstream, by the per-provider gate in
        # ask() (_provider_gate) — no separate CLI queue here.
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [*claude_cli.claude_cmd(), "-p", "--model", model,
                 *extra, *mcp],
                input=prompt, capture_output=True, text=True,
                timeout=timeout, cwd=cwd or claude_cli.agent_cwd(), env=env)
        except subprocess.TimeoutExpired:
            # a HUNG claude CLI (huge MCP system prompt load) must be a
            # retriable provider failure, NOT a fatal escape — v19 died
            # here: the CLI hung 300s while OpenRouter was healthy, and
            # TimeoutExpired slipped past the chain's (QuotaExhausted,
            # RuntimeError) net and killed the run
            last = f"timed out after {timeout}s"
            _attempt(attempt, t0, "timeout", True, "timeout")
            continue
        except OSError as exc:
            last = f"spawn failed: {exc}"
            _attempt(attempt, t0, "spawn_error", True, "spawn")
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            _attempt(attempt, t0, 0, False)
            return claude_cli.strip_headroom_banner(proc.stdout)
        last = (proc.stderr or proc.stdout)[-300:]
        _attempt(attempt, t0, proc.returncode, True,
                 "empty" if not proc.stdout.strip() else "error")
    raise RuntimeError(f"claude CLI failed after {RETRIES} tries: {last}")


# ── OpenAI-compatible HTTP (Bifrost unified / OpenRouter / any) ──────────────
def _http_post(url: str, payload: dict, headers: dict,
               timeout: "int | None" = None) -> tuple[int, str]:
    """Tiny urllib POST; returns (status, body). Separated for test stubbing.
    ``timeout`` is the per-call wall cap (per-provider, resolved by the caller);
    it falls back to the shared TIMEOUT."""
    to = timeout or TIMEOUT
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=to) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                       # non-2xx
        return e.code, e.read().decode("utf-8", "replace")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        # A socket read/connect timeout (the gateway hung past TIMEOUT) or an
        # unreachable endpoint is a PROVIDER failure the chain must absorb — NOT
        # a crash. urllib raises a raw TimeoutError/URLError here (both OSError
        # subclasses), which ask()'s except clause did NOT catch, so a SINGLE
        # hung Bifrost/claude call killed the whole run (live v093/v095 died on
        # an UNCAUGHT TimeoutError on the very first decompose call). Re-raise as
        # the RuntimeError taxonomy ask() understands: a timeout -> reason
        # 'timeout' (retire after one + fall back), an unreachable endpoint ->
        # a legible error the next model in the chain absorbs.
        reason = getattr(e, "reason", e)
        if isinstance(e, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError(
                f"openai backend timed out after {to}s") from e
        raise RuntimeError(f"openai backend unreachable: {reason}") from e


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


# ── reasoning control (abstract `params.reasoning`) ─────────────────────────
# The abstract per-call knob is `params: {reasoning: "off"|"low"|"medium"|
# "high"}` — generative stages keep thinking ON, service stages (review
# verdicts, strict-JSON checks) turn it down/off. OpenAI-compatible reasoning
# models DIFFER in the wire form they honour, so ONE table below translates the
# abstract level into each provider's real request-body field at the single
# body-assembly point (_ask_openai). The raw `reasoning` key itself is NEVER
# forwarded (it is not in _OPENAI_PARAM_KEYS), only its translation.
#
# EXPERIMENT (2026-07-02, live Bifrost gateway, model xiaomimimo/mimo-v2.5,
# prompt "Reply with the single word: ok" + one arithmetic prompt, small
# max_tokens; reasoning arrives in the SEPARATE message.reasoning field, the
# content stays clean — no inline <think> over this path):
#   * reasoning_effort ("low")   -> SUPPORTED. Upstream pydantic accepts ONLY
#     'low'|'medium'|'high' (400 literal_error on 'none'); measured on one
#     fixed prompt: low=536 vs high=719 reasoning tokens. NO true "off" value.
#   * enable_thinking: false     -> accepted silently, reasoning stayed (79 t).
#   * reasoning: {effort: low} / {enabled: false} (OpenRouter form) -> accepted
#     silently, reasoning stayed (35/42 t).
#   * thinking: {type: disabled} -> accepted silently, reasoning stayed (111 t).
# => xiaomimimo: map levels to reasoning_effort; "off" degrades to the lowest
#    supported effort AND the universal off-path (system nudge + think strip).
_REASONING_LEVELS = ("off", "low", "medium", "high")
# provider -> (request-body key, {abstract level -> wire value}). A level
# missing from the map sends nothing for that provider; a provider missing
# from the table sends nothing at all — "off" still gets the universal
# nudge+strip fallback below, so the knob degrades honestly everywhere.
_REASONING_WIRE: dict = {
    # Xiaomi MiMo (via Bifrost): OpenAI-style reasoning_effort, no off literal —
    # see the experiment note above; "off" maps to the cheapest legal effort.
    "xiaomimimo": ("reasoning_effort", {"off": "low", "low": "low",
                                        "medium": "medium", "high": "high"}),
    # OpenAI-style providers: reasoning_effort with the plain level string.
    "openai": ("reasoning_effort", {"low": "low", "medium": "medium",
                                    "high": "high"}),
    # OpenRouter: nested `reasoning` object; supports a true disable.
    "openrouter": ("reasoning", {"off": {"enabled": False},
                                 "low": {"effort": "low"},
                                 "medium": {"effort": "medium"},
                                 "high": {"effort": "high"}}),
}
# Universal "off" fallback for providers that cannot (or may not) truly
# disable thinking: instruct the model up front...
_NO_REASONING_NUDGE = ("Answer immediately with the final answer only, "
                       "no reasoning preamble.")
# ...and cut any inline think block out of the reply on parse.
_THINK_RE = re.compile(r"(?is)<think>.*?</think>\s*")


def _reasoning_level(params: dict | None) -> str:
    """Normalise `params.reasoning` to one of _REASONING_LEVELS or ''.
    Why: the knob arrives from YAML where a bare `off` parses as boolean False
    (YAML 1.1 footgun) — treat it as the string "off"; anything unrecognised is
    ignored so a typo can never 400 a live call.
    Test: {'reasoning': False} -> 'off'; {'reasoning': 'HIGH'} -> 'high';
    {'reasoning': 'bogus'} and absent key -> ''."""
    v = (params or {}).get("reasoning")
    if v is None:
        return ""
    if v is False:
        return "off"
    s = str(v).strip().lower()
    return s if s in _REASONING_LEVELS else ""


def _strip_think(text: str) -> str:
    """Cut inline <think>…</think> reasoning blocks out of a reply.
    Why: with reasoning "off" a model that cannot truly disable thinking may
    still wrap deliberation in think tags inside `content`; strict-JSON
    consumers (review verdicts) must never see it.
    What: removes every closed think block; a dangling unclosed <think> tail
    (truncated generation) is cut only when real text precedes it. If stripping
    would empty the reply, the original text is returned — the answer may live
    INSIDE the block and an empty reply would fail the call for nothing.
    Test: '<think>x</think>ok' -> 'ok'; 'ok' -> 'ok'; '<think>only' unchanged."""
    out = _THINK_RE.sub("", text)
    m = re.search(r"(?i)<think>", out)
    if m and out[:m.start()].strip():
        out = out[:m.start()]
    out = out.strip()
    return out if out else text


def _message_text(msg: dict) -> str:
    """Normalize an OpenAI-shaped chat message to plain reply text.
    Why: providers disagree on the `content` field shape — most return a
    string, some return a LIST of content parts ([{"type": "text",
    "text": ...}, ...]). v158 (S10.28): a list-shaped content hit
    ``text.strip()`` -> AttributeError, escaped the narrow shape-except AND
    ask()'s RuntimeError-only chain except, killed the orchestra coder/tester
    steps ('list' object has no attribute 'strip') — the about_page leaf
    delivered nothing and the doctor cause empty_delta stayed open all run.
    What: flattens str / list-of-parts / part dicts (text|content keys) to one
    string; falls back to `reasoning` (weak models answer there); anything
    unrecognizable yields "" so the caller's 'empty completion' retry path
    runs — a shape surprise degrades, never crashes.
    Test: tests/audit/test_provider_reply_shape.py (real local HTTP server)."""
    def _flat(v) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "".join(_flat(p) for p in v)
        if isinstance(v, dict):
            return _flat(v.get("text") or v.get("content") or "")
        return ""
    return _flat(msg.get("content")) or _flat(msg.get("reasoning"))


def _provider_of(model: str) -> str:
    """The provider key a model routes through — the prefix before '/'
    (openrouter / xiaomimimo / claude), else the default openai backend. Used to
    group response/failure stats by provider on the dashboard."""
    return model.split("/", 1)[0] if "/" in model else "openai"


def _failure_reason(exc: "Exception | str") -> str:
    """Classify why a model did not answer normally, for the failure report:
    429 / 5xx / timeout / empty / error. Universal — string-based, no provider
    special-casing."""
    s = str(exc).lower()
    if "throttl" in s or "429" in s or "quota" in s:
        return "429"
    # An explicit HTTP STATUS is classified by the status FIRST — before the
    # word "timeout" — so a server "504 gateway timeout" stays a 5xx (a discrete,
    # maybe-transient error that waits the breaker threshold) and is not confused
    # with a CLIENT timeout (a hung call we already paid for, retired after one).
    # Surface a specific 4xx code (e.g. 404 'no such agent') instead of a generic
    # 'error' so an unreachable hermes/MC agent stays legible.
    mm = re.search(r"http (\d\d\d)", s)
    if mm:
        code = mm.group(1)
        if code[0] == "5":
            return "5xx"
        # 401/403 == broken provider auth (a stale/missing key at the gateway):
        # it will NOT heal mid-run, so it is its own bucket and the breaker
        # retires the model after ONE (live v094: xiaomimimo 401'd 16x, each an
        # instant but wasted fallback). A generic 4xx stays a legible `http NNN`.
        if code in ("401", "403"):
            return "auth"
        return f"http {code}"
    # no HTTP status -> a transport-level outcome: a client timeout (the call
    # hung past the cap) or an empty/garbled body.
    if "timeout" in s or "timed out" in s:
        return "timeout"
    if "empty" in s or "malformed" in s or "bad response" in s:
        return "empty"
    return "error"


def _log_event(event: dict) -> None:
    """Best-effort llm-log write — logging the timeline of a call must never
    raise into the hot retry path."""
    try:
        from . import llm_log
        llm_log.log(event)
    except Exception:              # noqa: BLE001
        pass


def _ask_openai(prompt: str, model: str, system: str | None = None,
                params: dict | None = None, base_url: str | None = None,
                api_key: str | None = None) -> str:
    # base_url/api_key default to the module config, but a caller (e.g. the
    # claude-via-gateway route) may point ONE call at a different OpenAI-
    # compatible endpoint without touching the globals.
    url = (base_url or BASE_URL).rstrip("/") + "/chat/completions"
    _key = api_key if api_key is not None else API_KEY
    headers = {"Authorization": f"Bearer {_key}"} if _key else {}
    provider = _provider_of(model)
    body_params = _openai_params(params)
    # reasoning control: translate the abstract level into THIS provider's wire
    # form (see _REASONING_WIRE + the experiment note above it). On "off" the
    # universal fallback also applies: a no-reasoning system nudge here and a
    # think-block strip at parse time — harmless where the wire param already
    # disables thinking, essential where it cannot (xiaomimimo).
    r_lvl = _reasoning_level(params)
    if r_lvl:
        wire = _REASONING_WIRE.get(provider)
        if wire and r_lvl in wire[1]:
            body_params[wire[0]] = wire[1][r_lvl]
        if r_lvl == "off":
            system = ((system + "\n\n") if system else "") + _NO_REASONING_NUDGE
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages, **body_params}
    call_timeout = _provider_timeout(model)
    last = ""
    throttled = 0
    for attempt in range(1, RETRIES + 1):
        t0 = time.monotonic()
        status, body = _http_post(url, payload, headers, timeout=call_timeout)
        latency = round(time.monotonic() - t0, 2)
        err = ""
        if status == 200:
            try:
                out = json.loads(body)
                msg = out["choices"][0]["message"]
                # S10.28: ONE normalizer for the content shape (string, list
                # of parts, part dicts; weak reasoning models answer in
                # `reasoning` with an empty `content` — accepted too)
                text = _message_text(msg)
                if r_lvl == "off":
                    # off-fallback tail: a model that ignored the wire param /
                    # nudge may still emit an inline think block — cut it.
                    text = _strip_think(text)
                if text and text.strip():
                    # stash the REAL usage so ask()'s single exit point logs it
                    # (or falls back to tiktoken). Token accounting is universal
                    # in ask(), not per-backend here.
                    _call_ctx.last_usage = out.get("usage") or {}
                    _log_event({"event": "llm_attempt", "backend": "openai",
                                "provider": provider, "model": model,
                                "attempt": attempt, "status": 200,
                                "latency_s": latency, "abnormal": False})
                    return text
                last = "empty completion"
                err = "empty"
            except (KeyError, IndexError, json.JSONDecodeError,
                    TypeError, AttributeError) as exc:
                # TypeError/AttributeError included (S10.28): any residual
                # provider-shape surprise degrades to a retryable bad-shape
                # attempt — it must never escape as an uncaught crash that
                # kills the calling step (v158 orchestra coder/tester)
                last = f"bad response shape: {exc}: {body[-200:]}"
                err = "empty"
        else:
            last = f"HTTP {status}: {body[-200:]}"
            err = f"http_{status}"
        # free-pool throttling (429): honour the server-suggested pause when
        # present (OpenRouter sends retry_after_seconds), else back off harder
        if status == 429:
            throttled += 1
            m = re.search(r'"retry_after_seconds"\s*:\s*([0-9.]+)', body)
            slept = min(90.0, float(m.group(1)) + 2) if m else BACKOFF * attempt
        else:
            slept = float(BACKOFF)
        # log EVERY abnormal attempt so the dashboard can show all delay causes:
        # status, latency, the backoff/429 sleep, and what went wrong
        _log_event({"event": "llm_attempt", "backend": "openai",
                    "provider": provider, "model": model, "attempt": attempt,
                    "status": status, "latency_s": latency,
                    "slept_s": round(slept, 2), "abnormal": True,
                    "error": err})
        # A 5xx (esp. 504 gateway timeout) means THIS model/gateway is down or
        # overloaded — retrying the SAME model just burns another full timeout
        # (v063: mimo-v2.5 returned 504 ~121s each, hammered 6x = ~12min before
        # the chain advanced). Stop retrying and let ask() fall over to the next
        # model in the chain immediately. 429 (quota) still backs off/retries —
        # there the model is fine, only throttled.
        if 500 <= status < 600:
            last = f"HTTP {status} (gateway/server error): {body[-160:]}"
            break
        time.sleep(slept)
    if throttled:
        # ANY 429 among the attempts is a quota signal (the per-minute
        # ceiling of the free pool killed a whole run once: the final 429
        # surfaced as a plain RuntimeError and no fallback engaged)
        raise QuotaExhausted(
            f"free pool throttled ({throttled}/{RETRIES} tries): {last}")
    raise RuntimeError(f"openai backend failed after {RETRIES} tries: {last}")
