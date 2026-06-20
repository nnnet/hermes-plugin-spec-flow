"""Hermes adapter (doc §4): run a RoleTask on a Hermes agent/profile.

Why: a role (or one orchestra specialist) can be filled by a Hermes agent that
already carries skills/tools, instead of a raw local LLM — same RoleResult out.
What: POST the RoleTask as a single message to a Hermes gateway addressed to an
``agent`` (profile), read the reply + any written files, map them to a
RoleResultLike.
Test: a fake transport asserts the POST url (``{gateway}/v1/agents/{agent}/messages``),
method and JSON body (carries role/node/spec/prompt), and a canned reply with
``files`` parses into RoleResultLike.artifacts.

Config (resolved by the caller, passed on the RoleTask.context or constraints):
  - ``gateway``  — Hermes gateway base URL
  - ``agent``    — the target Hermes agent/profile id
Auth/transport live INSIDE this adapter (a bearer header when a token is given),
never in the role schema.
"""
from __future__ import annotations

import os
from typing import Optional

from .base import (Provider, RoleResultLike, RoleTaskLike, Transport,
                   json_request, register)


def _gateway(task: RoleTaskLike) -> str:
    """The Hermes gateway base URL for this task, from its context/constraints."""
    cfg = {**(task.constraints or {}), **(task.context or {})}
    url = str(cfg.get("gateway") or cfg.get("endpoint") or "").rstrip("/")
    if not url:
        raise ValueError("hermes provider needs a 'gateway' url in the task config")
    return url


def _agent(task: RoleTaskLike) -> str:
    cfg = {**(task.constraints or {}), **(task.context or {})}
    agent = str(cfg.get("agent") or "")
    if not agent:
        raise ValueError("hermes provider needs an 'agent' id in the task config")
    return agent


def _message(task: RoleTaskLike) -> str:
    """The role work, rendered as a single instruction message for the agent."""
    parts = [f"You are filling the '{task.role}' role for node '{task.node}'"
             f" ({task.title}).",
             "Produce the role's artifacts (files) and reply with a JSON object"
             ' {"files": {"<path>": "<content>", ...}}.']
    if task.spec:
        parts.append(f"Spec reference: {task.spec}")
    return "\n".join(parts)


@register("hermes")
class HermesProvider(Provider):
    """Adapter for a Hermes agent/profile (doc §4 row `hermes`)."""

    def execute(self, task: RoleTaskLike) -> RoleResultLike:
        """POST the RoleTask to the agent, map the reply → RoleResultLike.

        Test: fake transport asserts method=POST, url ends
        ``/v1/agents/<agent>/messages``, body has role/node/message; a canned
        ``{"files": {...}}`` reply yields those artifacts."""
        gateway, agent = _gateway(task), _agent(task)
        cfg = {**(task.constraints or {}), **(task.context or {})}
        # auth lives INSIDE the adapter: explicit cfg token, else the operator's
        # env (GATEWAY_API_KEY) — never a secret in the role schema or YAML
        headers: dict = {}
        token = cfg.get("token") or os.environ.get("GATEWAY_API_KEY")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "role": task.role, "node": task.node, "title": task.title,
            "spec": task.spec, "specialty": task.specialty,
            "params": task.params or {}, "message": _message(task),
        }
        url = f"{gateway}/v1/agents/{agent}/messages"
        status, body = json_request(self.transport, url, method="POST",
                                    payload=payload, headers=headers)
        if status >= 400:
            raise RuntimeError(f"hermes agent {agent} returned HTTP {status}: "
                               f"{str(body)[:200]}")
        return _parse_reply(body, agent)


def _parse_reply(body, agent: str) -> RoleResultLike:
    """Map a Hermes reply object into a RoleResultLike.

    Accepts either ``{"files": {...}}`` directly or ``{"reply": "...", "files":
    {...}}``; a bare verdict ``{"pass": bool}`` becomes a verdict result."""
    if not isinstance(body, dict):
        return RoleResultLike(kind="files", artifacts={},
                              meta={"provider": "hermes", "agent": agent,
                                    "raw": str(body)[:300]})
    files = body.get("files")
    verdict = body.get("verdict")
    if verdict is None and "pass" in body:
        verdict = {"pass": bool(body.get("pass")),
                   "reasons": body.get("reasons") or []}
    meta = {"provider": "hermes", "agent": agent}
    if "reply" in body:
        meta["reply"] = str(body["reply"])[:1000]
    return RoleResultLike(
        kind="verdict" if verdict is not None and not files else "files",
        artifacts={str(k): str(v) for k, v in (files or {}).items()},
        verdict=verdict, meta=meta)
