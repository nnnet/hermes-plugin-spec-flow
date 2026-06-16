"""Executor roster + task->executor routing (C1).

Where #10 (`mem:` specialty.py) routes a leaf to a better-fit MODEL CHAIN, this
module routes a leaf to a better-fit EXECUTOR: a named agent OR a D1 implementer
team-spec (architect/coder/tester/fixer). The two are orthogonal — specialty
picks the model, the executor picks WHO (which roster member) builds the leaf.

Config (in the case `workers:` block, read from llm_backend.WORKERS_CFG):

    workers.executors:
      default: {team: {specialists: [...]}}   # the general executor
      frontend: web-agent                      # a named agent for web leaves
      database: {team: {specialists: [...]}}   # a domain-specific D1 team

Resolution per leaf, highest priority first:
  1. EXPLICIT node['domain'] (the decomposer or a human pinned it),
  2. project default_domain,
  3. AUTO from the node's title+spec (reusing the specialty anchors),
  4. 'general' — the catch-all routed to executors['default'] (or, absent that,
     today's single-agent path).

An UNKNOWN domain never fails: it falls back to the default executor, and a
missing default falls back to None (the engine's normal implementer). Test:
tests/workers/test_executor_routing.py — pure, offline, no LLM.
"""
from __future__ import annotations

from typing import Any, Optional

try:  # reuse the specialty anchors so domain == specialty vocabulary
    from . import specialty as _sp
except Exception:  # noqa: BLE001 — standalone import fallback
    import specialty as _sp  # type: ignore

GENERAL = "general"


def infer_domain(title: str, spec: str = "") -> str:
    """The leaf's domain inferred from its title + spec text, or 'general' when
    nothing scores. Reuses the specialty anchors (frontend/database/api/auth/
    payments/messaging) so one semantic map serves both axes."""
    return _sp.infer_specialty(title, spec) or GENERAL


def resolve_domain(node: dict, project: Optional[dict], auto: bool) -> str:
    """Pick a leaf's domain: explicit node['domain'] > project default_domain >
    (when auto) inferred > 'general'. Always returns a non-empty domain."""
    explicit = str((node or {}).get("domain", "") or "").strip()
    if explicit:
        return explicit
    pinned = str((project or {}).get("default_domain", "") or "").strip()
    if pinned:
        return pinned
    if auto:
        return infer_domain(node.get("title", ""),
                            node.get("spec_markdown", ""))
    return GENERAL


def parse_executors(workers_cfg: Any) -> dict:
    """The executor roster from a workers config (domain -> executor spec), or
    {} when none is configured. An executor spec is a named agent (str) or a D1
    team-spec (dict)."""
    if not isinstance(workers_cfg, dict):
        return {}
    execs = workers_cfg.get("executors")
    return execs if isinstance(execs, dict) else {}


def resolve_executor(domain: str, roster: dict) -> Any:
    """The executor spec for a domain: the roster's entry for it, else the
    roster's 'default', else None (route to the engine's normal implementer).
    An unknown domain is NOT an error — it degrades to default/None."""
    if not isinstance(roster, dict) or not roster:
        return None
    if domain in roster:
        return roster[domain]
    return roster.get("default")


def team_of(executor: Any) -> Optional[list]:
    """The D1 specialist LIST an executor spec carries, or None when the
    executor is a named agent / empty. Accepts the canonical
    `{team: {specialists: [...]}}`, a bare `{specialists: [...]}`, the legacy
    `{team: [...]}` list, or a top-level list — mirroring _implementer_team's
    shapes so a routed team behaves exactly like a globally-configured one."""
    spec: Any = executor
    if isinstance(spec, dict):
        if "team" in spec:
            spec = spec.get("team")
        if isinstance(spec, dict):
            spec = spec.get("specialists")
    return spec if isinstance(spec, list) and spec else None


def agent_of(executor: Any) -> Optional[str]:
    """The named agent an executor spec routes to, or None. A bare string is the
    agent name; a dict may carry it under 'agent' (a team-spec has none)."""
    if isinstance(executor, str) and executor.strip():
        return executor.strip()
    if isinstance(executor, dict):
        name = executor.get("agent")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None
