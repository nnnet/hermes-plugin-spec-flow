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

import json
import os
import re
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
        """Run the RoleTask on the Hermes agent via the gateway's OpenAI-compatible
        ``/v1/chat/completions`` and map the reply → RoleResultLike.

        The Hermes gateway IS its ``api_server`` platform: a chat completion sent
        to it is answered by the Hermes agent (carrying the api_server toolset /
        skills), so this is a real agent invocation, not a bare LLM proxy. The
        ``agent`` id labels the call (system framing + meta); the ``model`` from
        the task selects the gateway's backing model.

        Test: a fake transport asserts method=POST, url ends
        ``/v1/chat/completions``, body carries the chat ``messages`` (role/node in
        the user content); a canned chat reply whose content is ``{"files": {...}}``
        yields those artifacts."""
        gateway, agent = _gateway(task), _agent(task)
        cfg = {**(task.constraints or {}), **(task.context or {})}
        # auth lives INSIDE the adapter: explicit cfg token, else the operator's
        # env — API_SERVER_KEY is the gateway's inbound key (GATEWAY_API_KEY kept
        # as a legacy fallback) — never a secret in the role schema or YAML
        headers: dict = {}
        token = (cfg.get("token") or os.environ.get("API_SERVER_KEY")
                 or os.environ.get("GATEWAY_API_KEY"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        model = task.model or cfg.get("model") or "claude/haiku"
        system = (f"You are the Hermes '{agent}' agent filling the '{task.role}' "
                  f"role for node '{task.node}'.")
        payload: dict = {
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": _message(task)}],
        }
        if isinstance(task.params, dict):
            for k in ("temperature", "max_tokens"):
                if k in task.params:
                    payload[k] = task.params[k]
        url = f"{gateway}/v1/chat/completions"
        # the gateway runs the full Hermes agent loop (session init + tools), so
        # the first call can far exceed the transport's default; allow room.
        timeout = float(cfg.get("timeout") or os.environ.get(
            "SPEC_FLOW_HERMES_TIMEOUT", "180"))
        status, body = json_request(self.transport, url, method="POST",
                                    payload=payload, headers=headers,
                                    timeout=timeout)
        if status >= 400:
            raise RuntimeError(f"hermes gateway returned HTTP {status}: "
                               f"{str(body)[:200]}")
        return _parse_chat_reply(body, agent)


def _extract_json_obj(text: str) -> Optional[dict]:
    """The first JSON object embedded in assistant text (``{...}``), tolerating a
    ```json fence or leading prose; None when there is none."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else None
    if cand is None:
        i, j = text.find("{"), text.rfind("}")
        cand = text[i:j + 1] if (i != -1 and j > i) else None
    if not cand:
        return None
    try:
        out = json.loads(cand)
        return out if isinstance(out, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_chat_reply(body, agent: str) -> RoleResultLike:
    """Map an OpenAI chat-completion reply from the Hermes gateway →
    RoleResultLike.

    The assistant content may carry a ``{"files": {...}}`` / ``{"pass": bool}``
    object (the message asked for it); otherwise the raw text is kept as the
    reply so the caller still has the agent's answer."""
    content = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = str((choices[0].get("message") or {}).get("content") or "")
    elif isinstance(body, str):
        content = body
    parsed = _extract_json_obj(content)
    files = parsed.get("files") if isinstance(parsed, dict) else None
    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    if isinstance(parsed, dict) and verdict is None and "pass" in parsed:
        verdict = {"pass": bool(parsed.get("pass")),
                   "reasons": parsed.get("reasons") or []}
    meta = {"provider": "hermes", "agent": agent, "reply": content[:1000]}
    return RoleResultLike(
        kind="verdict" if verdict is not None and not files else "files",
        artifacts={str(k): str(v) for k, v in (files or {}).items()},
        verdict=verdict, meta=meta)
