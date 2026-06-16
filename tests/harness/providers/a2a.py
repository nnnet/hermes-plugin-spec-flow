"""A2A adapter (doc §4): run a RoleTask on an external agent over the A2A
protocol — the open standard for standing-up an external agent/service.

Why: rather than invent a bespoke RPC, we adopt A2A (Agent Cards + task
lifecycle + artifacts over JSON-RPC/HTTP). Any A2A-compliant service can fill a
role or a whole external team with no plugin-side knowledge of its internals.
What:
  1. discover the Agent Card at ``{endpoint}/.well-known/agent.json`` (skipped
     when a card is supplied on the task config);
  2. ``tasks/send`` a JSON-RPC message carrying the RoleTask as a text part;
  3. poll ``tasks/get`` until the task state is terminal
     (submitted→working→completed / failed / canceled);
  4. collect the task's ``artifacts`` (file parts) → RoleResultLike.artifacts.
Streaming (SSE) is optional; polling is sufficient and used here.
Test: a fake transport serves the Agent Card on the well-known GET, asserts the
``tasks/send`` JSON-RPC envelope (method + the RoleTask text part + an id), then
serves ``tasks/get`` going working→completed with file artifacts, which parse
into RoleResultLike.artifacts.

Config (on the task context/constraints):
  - ``endpoint``    — the A2A service base URL
  - ``agent_card``  — optional pre-fetched card (skips discovery)
  - ``token``       — optional bearer
  - ``poll_attempts`` — optional cap
"""
from __future__ import annotations

import json
from typing import Optional

from .base import (Provider, RoleResultLike, RoleTaskLike, json_request,
                   register)

_WELL_KNOWN = "/.well-known/agent.json"
_TERMINAL_DONE = {"completed"}
_TERMINAL_FAIL = {"failed", "canceled", "cancelled", "rejected"}


def _cfg(task: RoleTaskLike) -> dict:
    return {**(task.constraints or {}), **(task.context or {})}


def _rpc(method: str, params: dict, rpc_id: str) -> dict:
    """A JSON-RPC 2.0 request envelope for the A2A method."""
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def _task_message(task: RoleTaskLike) -> dict:
    """The A2A message: a single text part carrying the serialised RoleTask."""
    text = json.dumps({
        "role": task.role, "node": task.node, "title": task.title,
        "spec": task.spec, "specialty": task.specialty,
        "params": task.params or {},
    })
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


@register("a2a")
class A2AProvider(Provider):
    """Adapter speaking the A2A protocol (doc §4 row `a2a`)."""

    def execute(self, task: RoleTaskLike) -> RoleResultLike:
        cfg = _cfg(task)
        endpoint = str(cfg.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ValueError("a2a provider needs an 'endpoint' url")
        headers: dict = {}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"

        card = cfg.get("agent_card") or self._discover_card(endpoint, headers)
        rpc_url = self._rpc_url(endpoint, card)
        a2a_id = f"{task.role}-{task.node}"

        # 1) tasks/send — submit the role work as a message
        status, body = json_request(
            self.transport, rpc_url, method="POST",
            payload=_rpc("tasks/send",
                         {"id": a2a_id, "message": _task_message(task)}, a2a_id),
            headers=headers)
        if status >= 400:
            raise RuntimeError(f"a2a tasks/send HTTP {status}: {str(body)[:200]}")
        result = _result_of(body)
        state = _state_of(result)

        # 2) poll tasks/get until terminal (send may already be terminal)
        attempts = int(cfg.get("poll_attempts") or 30)
        polls = 0
        while state not in _TERMINAL_DONE and state not in _TERMINAL_FAIL:
            if polls >= max(1, attempts):
                raise RuntimeError(
                    f"a2a task {a2a_id} not terminal after {attempts} polls")
            polls += 1
            st, pb = json_request(
                self.transport, rpc_url, method="POST",
                payload=_rpc("tasks/get", {"id": a2a_id}, a2a_id),
                headers=headers)
            if st >= 400:
                raise RuntimeError(f"a2a tasks/get HTTP {st}")
            result = _result_of(pb)
            state = _state_of(result)

        if state in _TERMINAL_FAIL:
            return RoleResultLike(
                kind="verdict",
                verdict={"pass": False, "reasons": [f"a2a state {state}"]},
                meta={"provider": "a2a", "endpoint": endpoint,
                      "task_id": a2a_id, "state": state})
        return _collect_artifacts(result, endpoint, a2a_id)

    def _discover_card(self, endpoint: str, headers: dict) -> dict:
        """Fetch the Agent Card from the well-known path (A2A discovery).

        Test: fake transport serves a card on GET ``/.well-known/agent.json``."""
        status, body = json_request(self.transport, endpoint + _WELL_KNOWN,
                                    method="GET", headers=headers)
        if status >= 400 or not isinstance(body, dict):
            raise RuntimeError(
                f"a2a agent-card discovery failed HTTP {status} at {endpoint}")
        return body

    @staticmethod
    def _rpc_url(endpoint: str, card: dict) -> str:
        """The JSON-RPC endpoint: the Agent Card's ``url`` if present, else the
        base endpoint (A2A serves RPC at the advertised url)."""
        url = str((card or {}).get("url") or endpoint).rstrip("/")
        return url or endpoint


def _result_of(body) -> dict:
    """The JSON-RPC ``result`` (the A2A Task object) from a response envelope."""
    if isinstance(body, dict):
        res = body.get("result")
        if isinstance(res, dict):
            return res
        if "status" in body or "artifacts" in body:   # already unwrapped
            return body
    return {}


def _state_of(result: dict) -> str:
    """The A2A task state, from ``status.state`` (or a flat ``state``)."""
    status = result.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "").lower()
    return str(result.get("state") or status or "").lower()


def _collect_artifacts(result: dict, endpoint: str, task_id: str) -> RoleResultLike:
    """Flatten A2A artifacts (each with file parts) → {path: text}.

    An A2A artifact is ``{name, parts: [{type: file, file: {name, bytes|text}}|
    {type: text, text}]}``; we keep file/text parts keyed by the file name (or
    the artifact name)."""
    files: dict = {}
    for art in result.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        art_name = str(art.get("name") or "")
        for part in art.get("parts") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "file":
                f = part.get("file") or {}
                name = str(f.get("name") or art_name)
                text = f.get("text")
                if text is None and f.get("bytes") is not None:
                    text = _decode_bytes(f["bytes"])
                if name and text is not None:
                    files[name] = str(text)
            elif ptype == "text" and art_name:
                files[art_name] = str(part.get("text") or "")
    return RoleResultLike(
        kind="files", artifacts=files,
        meta={"provider": "a2a", "endpoint": endpoint, "task_id": task_id,
              "state": "completed"})


def _decode_bytes(value) -> str:
    """A2A file ``bytes`` are base64; decode to text (best-effort)."""
    import base64
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return str(value)
