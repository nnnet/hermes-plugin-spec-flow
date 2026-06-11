"""Console HITL approver — a real human checkpoint without Hermes.

The engine consults ``agents["approver"]`` at every human-in-the-loop
checkpoint. The autonomous default rubber-stamps with an honest note; this
module puts an actual human in the loop through the terminal. A Telegram
approver is the Hermes-integration variant and lives outside the plugin —
hook an external command via SPEC_FLOW_HITL_CMD when needed.

Env knobs:
  SPEC_FLOW_HITL_TIMEOUT   seconds to wait for the human (default 120);
                           timeout/no-tty auto-approves WITH an honest note
  SPEC_FLOW_HITL_CMD       optional external command; gets kind/task/detail
                           as argv, exit 0 = approved, non-zero = rejected,
                           stdout (first line) = reason
"""
from __future__ import annotations

import os
import select
import subprocess
import sys


TIMEOUT = float(os.environ.get("SPEC_FLOW_HITL_TIMEOUT", "120"))


def console_approver(ctx: dict) -> dict:
    """Ask the human at the terminal; on timeout/no-tty approve with a note."""
    kind = ctx.get("kind", "node")
    task = ctx.get("task", "?")
    detail = ctx.get("detail", "")

    cmd = os.environ.get("SPEC_FLOW_HITL_CMD", "").strip()
    if cmd:
        proc = subprocess.run([cmd, kind, task, detail],
                              capture_output=True, text=True, timeout=TIMEOUT + 30)
        reason = (proc.stdout or "").strip().splitlines()[:1]
        return {"approved": proc.returncode == 0,
                "reason": (reason[0] if reason else f"external approver rc={proc.returncode}")}

    if not sys.stdin.isatty():
        return {"approved": True,
                "reason": "no tty attached — auto-approved (honest note)"}

    sys.stderr.write(
        f"\n[HITL] {kind} checkpoint at task '{task}'\n"
        f"       {detail}\n"
        f"       approve? [Y/n] (auto-approve in {int(TIMEOUT)}s): ")
    sys.stderr.flush()
    ready, _, _ = select.select([sys.stdin], [], [], TIMEOUT)
    if not ready:
        sys.stderr.write("\n[HITL] timeout — auto-approved with note\n")
        return {"approved": True, "reason": f"human silent for {int(TIMEOUT)}s — auto-approved"}
    answer = sys.stdin.readline().strip().lower()
    approved = answer in ("", "y", "yes", "д", "да")
    reason = "approved by human at console" if approved else \
        f"REJECTED by human at console{': ' + answer if answer not in ('n', 'no') else ''}"
    return {"approved": approved, "reason": reason}
