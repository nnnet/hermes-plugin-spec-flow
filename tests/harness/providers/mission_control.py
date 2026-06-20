"""Mission-Control adapter (doc §4): run a RoleTask as an MC task.

Why: a role/specialist can be an MC agent (or agent-template) so an existing
crew member does the work; MC owns task lifecycle, we just create + poll.
What: create an MC task assigned to an ``agent``/``agent_template`` via the MC
API base, poll the task until it reaches a terminal state, map its output →
RoleResultLike.
Test: a fake transport asserts the create POST (``{api}/api/tasks`` with the
assignee + the RoleTask payload), then serves a sequence of poll responses
(running → done) and the done output parses into artifacts.

Config (on the task context/constraints):
  - ``api``            — MC API base url
  - ``agent`` OR ``agent_template`` — the assignee
  - ``poll_attempts``  — optional cap (default small; tests inject 'done' first)
"""
from __future__ import annotations

import os
from typing import Optional

from .base import (Provider, RoleResultLike, RoleTaskLike, json_request,
                   register)

_TERMINAL_DONE = {"done", "completed", "complete", "succeeded", "success"}
_TERMINAL_FAIL = {"failed", "error", "cancelled", "canceled"}


def _cfg(task: RoleTaskLike) -> dict:
    return {**(task.constraints or {}), **(task.context or {})}


@register("mission-control")
class MissionControlProvider(Provider):
    """Adapter for a Mission-Control agent/template (doc §4 row `mission-control`)."""

    def execute(self, task: RoleTaskLike) -> RoleResultLike:
        """Create an MC task, poll to completion, map output → RoleResultLike.

        Test: fake transport asserts POST ``/api/tasks`` carries the assignee +
        RoleTask, then GET ``/api/tasks/<id>`` is polled; a 'done' poll with
        ``output.files`` yields those artifacts."""
        cfg = _cfg(task)
        api = str(cfg.get("api") or cfg.get("endpoint") or "").rstrip("/")
        if not api:
            raise ValueError("mission-control provider needs an 'api' base url")
        assignee = cfg.get("agent") or cfg.get("agent_template")
        if not assignee:
            raise ValueError(
                "mission-control provider needs 'agent' or 'agent_template'")
        # auth lives INSIDE the adapter: explicit cfg key, else the operator's
        # env (HERMES_MC_API_KEY) — never a secret in the role schema or YAML
        headers: dict = {}
        api_key = cfg.get("api_key") or os.environ.get("HERMES_MC_API_KEY")
        if api_key:
            headers["X-API-Key"] = str(api_key)

        create = {
            "title": f"{task.role}:{task.node}",
            "assignee": str(assignee),
            "role": task.role, "node": task.node,
            "spec": task.spec, "specialty": task.specialty,
            "params": task.params or {},
        }
        if cfg.get("agent_template"):
            create["agent_template"] = str(cfg["agent_template"])
        status, body = json_request(self.transport, f"{api}/api/tasks",
                                    method="POST", payload=create,
                                    headers=headers)
        if status >= 400 or not isinstance(body, dict):
            raise RuntimeError(f"mission-control create failed HTTP {status}: "
                               f"{str(body)[:200]}")
        task_id = body.get("id") or body.get("task_id")
        if not task_id:
            raise RuntimeError("mission-control create returned no task id")

        attempts = int(cfg.get("poll_attempts") or 30)
        for _ in range(max(1, attempts)):
            st, poll = json_request(self.transport,
                                    f"{api}/api/tasks/{task_id}",
                                    method="GET", headers=headers)
            if st >= 400 or not isinstance(poll, dict):
                raise RuntimeError(f"mission-control poll failed HTTP {st}")
            state = str(poll.get("status") or poll.get("state") or "").lower()
            if state in _TERMINAL_FAIL:
                return RoleResultLike(
                    kind="verdict",
                    verdict={"pass": False,
                             "reasons": [str(poll.get("error") or state)]},
                    meta={"provider": "mission-control", "agent": str(assignee),
                          "task_id": str(task_id), "state": state})
            if state in _TERMINAL_DONE:
                return _parse_output(poll, str(assignee), str(task_id))
        raise RuntimeError(
            f"mission-control task {task_id} did not finish in {attempts} polls")


def _parse_output(poll: dict, assignee: str, task_id: str) -> RoleResultLike:
    """Map a terminal MC task body → RoleResultLike.

    Output may live under ``output`` (dict with ``files``/``verdict``) or be a
    flat ``files`` map on the task itself."""
    output = poll.get("output")
    if not isinstance(output, dict):
        output = poll
    files = output.get("files") or {}
    verdict = output.get("verdict")
    meta = {"provider": "mission-control", "agent": assignee,
            "task_id": task_id, "state": "done"}
    return RoleResultLike(
        kind="verdict" if verdict is not None and not files else "files",
        artifacts={str(k): str(v) for k, v in files.items()},
        verdict=verdict, meta=meta)
