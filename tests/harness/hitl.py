"""Human-in-the-loop without Hermes: approver + two-way dialogue channel.

Approve/decline checkpoints are the BASE level (``console_approver``).
``HumanChannel`` adds genuine participation on top:

  * a worker that hits a real blocker may ASK the human a clarifying
    question, get the answer and fold it into its work;
  * the human watching the run may interject a note/question/order at any
    moment (inbox file); the next worker must address it — either DEFEND
    its course with arguments (status quo preserved) or COMPLY and treat
    the note as a directive. Every reply lands in the outbox.

Transport is deliberately boring — files in ``<run>/hitl/``:
  inbox.md       ← the HUMAN writes here while the run goes
  outbox.md      → workers' comply/defend replies to operator notes
  questions.md   → audit log of worker questions
  answer.md      ← the HUMAN answers the pending worker question here
A tty session prompts on the terminal instead of polling files. A
Telegram bridge is the Hermes-integration variant and stays outside the
plugin — hook an external command via SPEC_FLOW_HITL_CMD when needed.

Env knobs:
  SPEC_FLOW_HITL_TIMEOUT   seconds to wait for the human (default 120);
                           timeout/no-tty auto-approves WITH an honest note
  SPEC_FLOW_HITL_ASK_TIMEOUT  seconds to wait for an ANSWER to a worker
                           question (default 300); on timeout the worker is
                           told to proceed on its own judgement
  SPEC_FLOW_HITL_CMD       optional external command; gets kind/task/detail
                           as argv, exit 0 = approved, non-zero = rejected,
                           stdout (first line) = reason
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


TIMEOUT = float(os.environ.get("SPEC_FLOW_HITL_TIMEOUT", "120"))
ASK_TIMEOUT = float(os.environ.get("SPEC_FLOW_HITL_ASK_TIMEOUT", "300"))
_POLL_SEC = 3.0


class HumanChannel:
    """Two-way human side-channel for one run (file-based + tty fast path)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.inbox = self.root / "inbox.md"
        self.outbox = self.root / "outbox.md"
        self.questions = self.root / "questions.md"
        self.answer = self.root / "answer.md"
        # standing human requirements (may be added MID-RUN): one folder per
        # requirement under hitl/requirements/<name>/ holding REQUIREMENT.md
        # (the statement, shown to every branch decomposer) and test_*.py
        # acceptance tests (synced into the workspace smoke suite — the root
        # integrate cannot go green until the requirement is REAL)
        self.requirements_dir = self.root / "requirements"

    # -- worker → human ---------------------------------------------------
    def ask(self, role: str, node: str, question: str) -> Optional[str]:
        """Blocking clarification request. Returns the human's answer, or
        None when the human stayed silent (worker proceeds on its own)."""
        stamp = time.strftime("%H:%M:%S")
        with open(self.questions, "a", encoding="utf-8") as fh:
            fh.write(f"\n## [{stamp}] {role} @ {node}\n{question}\n")
        if sys.stdin.isatty():
            sys.stderr.write(
                f"\n[HITL?] {role} @ {node} asks:\n  {question}\n"
                f"  answer (empty = let it decide, {int(ASK_TIMEOUT)}s): ")
            sys.stderr.flush()
            ready, _, _ = select.select([sys.stdin], [], [], ASK_TIMEOUT)
            ans = sys.stdin.readline().strip() if ready else ""
        else:
            # background run: the human answers by writing hitl/answer.md
            sys.stderr.write(
                f"[HITL?] {role} @ {node} asks: {question}\n"
                f"        answer file: {self.answer} "
                f"(waiting {int(ASK_TIMEOUT)}s)\n")
            deadline = time.time() + ASK_TIMEOUT
            ans = ""
            while time.time() < deadline:
                if self.answer.exists():
                    ans = self.answer.read_text(encoding="utf-8").strip()
                    if ans:
                        self.answer.unlink(missing_ok=True)
                        break
                time.sleep(_POLL_SEC)
        with open(self.questions, "a", encoding="utf-8") as fh:
            fh.write(f"**answer:** {ans or '(none — worker proceeds on its own)'}\n")
        return ans or None

    # -- human → workers ---------------------------------------------------
    def poll_note(self, branch_capable: bool = False) -> Optional[str]:
        """Read-and-consume the operator's pending note, if any.

        A note starting with ``@branch`` is ADDRESSED: only a worker that
        can act on tree structure (a branch-capable decomposer) consumes
        it — atomic-depth nodes leave it in place instead of burning it on
        a powerless 'cannot act' reply (observed live: four deliveries of a
        web-ui requirement eaten by leaves that could do nothing).

        An orphan ``answer.md`` (the human answered a worker question AFTER
        its waiting window expired) is consumed here too — a late answer is
        still heard, as an operator note to the next worker."""
        note = ""
        if self.inbox.exists():
            note = self.inbox.read_text(encoding="utf-8").strip()
            if note.startswith("@branch") and not branch_capable:
                note = ""
            elif note:
                self.inbox.write_text("", encoding="utf-8")
        if not note and self.answer.exists():
            late = self.answer.read_text(encoding="utf-8").strip()
            if late:
                self.answer.unlink(missing_ok=True)
                note = ("late answer to an earlier worker question "
                        "(the wait window had expired): " + late)
        return note or None

    def record_reply(self, role: str, node: str, position: str,
                     response: str, note: str) -> None:
        """Worker's comply/defend reply to an operator note — audit trail."""
        stamp = time.strftime("%H:%M:%S")
        with open(self.outbox, "a", encoding="utf-8") as fh:
            fh.write(f"\n## [{stamp}] {role} @ {node} — {position.upper()}\n"
                     f"**operator:** {note}\n**worker:** {response}\n")

    # -- standing requirements (artifact-based, enforceable) ----------------
    def standing_requirements(self) -> list[tuple[str, str, Optional[str]]]:
        """[(name, statement, scope)] of every registered requirement.

        ``scope`` comes from an optional ``@scope: <node_id>`` line in
        REQUIREMENT.md: the ENGINE places a scoped requirement as a child
        of that branch while it is still open, anything else (or a missed
        scope) lands as a direct root child — the level whose integrate
        sees the assembled product."""
        import re as _re
        out = []
        if self.requirements_dir.is_dir():
            for d in sorted(self.requirements_dir.iterdir()):
                f = d / "REQUIREMENT.md"
                if d.is_dir() and f.is_file():
                    text = f.read_text(encoding="utf-8").strip()
                    m = _re.search(r"^@scope:\s*(\S+)", text, _re.M)
                    out.append((d.name, text, m.group(1) if m else None))
        return out

    def sync_requirements(self, ws_root: str | Path) -> list[str]:
        """Copy each requirement's acceptance tests into the workspace smoke
        suite (root-gate only, like the platform smoke) and mark them
        protected. A message can be ignored; a red acceptance test cannot —
        the root integrate stays FAIL until the requirement is REAL.

        Returns the workspace-relative paths synced (for logging)."""
        synced: list[str] = []
        if not self.requirements_dir.is_dir():
            return synced
        smoke = Path(ws_root) / "tests" / "smoke"
        for d in sorted(self.requirements_dir.iterdir()):
            if not d.is_dir():
                continue
            for t in sorted(d.glob("test_*.py")):
                rel = f"tests/smoke/acceptance_{d.name}_{t.name}"
                target = Path(ws_root) / rel
                body = t.read_text(encoding="utf-8")
                if not target.exists() or \
                        target.read_text(encoding="utf-8") != body:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body, encoding="utf-8")
                synced.append(rel)
        if synced:
            current = set()
            raw = os.environ.get("SPEC_FLOW_PROTECTED_FILES", "")
            try:
                current = set(json.loads(raw)) if raw else set()
            except json.JSONDecodeError:
                pass
            os.environ["SPEC_FLOW_PROTECTED_FILES"] = json.dumps(
                sorted(current | set(synced)))
        return synced


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
