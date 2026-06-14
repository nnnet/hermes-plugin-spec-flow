"""Autonomous HITL dispatcher — replays a declarative injection script against a
live run so no human has to babysit it.

The HITL channel (``tests/harness/hitl.py``) is purely file-based: the engine
polls ``<run>/hitl/`` for standing requirements and answers. A human normally
watches ``trace.jsonl`` and, at the right moment, drops the same files by hand
(create ``hitl/requirements/<name>/`` for a late requirement, write
``hitl/answer.md`` to reply to a blocked worker). This dispatcher does exactly
that, driven by the case's ``injections:`` block, on a daemon thread inside the
run process — it writes the very files a human would.

Declared in the case YAML::

    injections:
      requirements:
        - name: web_ui
          from: tests/scenarios/injections/web_ui   # repo-relative dir with
          when: {integrate_passes: 2}               # REQUIREMENT.md + test_*.py
      answers:
        default: "Proceed with the simplest correct approach; do not block."
        faq:
          - {match: "port", reply: "Bind 0.0.0.0:8092."}

Triggers (``when:``) — first match fires the requirement once:
  * ``integrate_passes: N`` — ≥N ``integrate_verify`` gates reached PASS
  * ``event: <substr>``     — a trace line's action/detail contains <substr>
  * ``start: true``         — fire immediately at run start

The dispatcher never touches the workers' code or the engine; it only writes
into ``<run>/hitl/``, identical to manual operation.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

# repo root = .../spec-flow  (this file is tests/lib/hitl_dispatcher.py)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLL_SEC = 1.0


class Dispatcher:
    """Watches a run and materialises declared HITL injections on its own."""

    def __init__(self, case_dir: str | Path, injections: dict | None) -> None:
        self.case_dir = Path(case_dir)
        self.trace = self.case_dir / "trace.jsonl"
        self.hitl = self.case_dir / "hitl"
        self.req_root = self.hitl / "requirements"
        self.answer = self.hitl / "answer.md"
        self.questions = self.hitl / "questions.md"
        inj = injections or {}
        self.requirements = list(inj.get("requirements") or [])
        ans = inj.get("answers") or {}
        self.answer_default = ans.get("default")
        self.answer_faq = list(ans.get("faq") or [])
        self._fired: set[str] = set()
        self._answered = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public lifecycle -------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.requirements or self.answer_default or self.answer_faq)

    def start(self) -> "Dispatcher":
        if not self.enabled():
            return self
        self.hitl.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="hitl-dispatcher",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- worker loop ------------------------------------------------------
    def _loop(self) -> None:
        # fire start-triggered requirements immediately
        for req in self.requirements:
            if (req.get("when") or {}).get("start"):
                self._fire_requirement(req)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — a dispatcher hiccup never kills the run
                pass
            time.sleep(_POLL_SEC)

    def _tick(self) -> None:
        passes, actions = self._scan_trace()
        for req in self.requirements:
            name = str(req.get("name") or "")
            if not name or name in self._fired:
                continue
            when = req.get("when") or {}
            if "integrate_passes" in when and passes >= int(when["integrate_passes"]):
                self._fire_requirement(req)
            elif when.get("event") and any(when["event"] in a for a in actions):
                self._fire_requirement(req)
        self._answer_pending()

    # -- trace reading ----------------------------------------------------
    def _scan_trace(self) -> tuple[int, list[str]]:
        """Return (#integrate_verify PASS, [action+detail strings])."""
        if not self.trace.is_file():
            return 0, []
        passes = 0
        actions: list[str] = []
        for ln in self.trace.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("gate") == "integrate_verify" and str(e.get("verdict")).upper() == "PASS":
                passes += 1
            actions.append(f"{e.get('action', '')} {e.get('detail', '')}")
        return passes, actions

    # -- actions ----------------------------------------------------------
    def _fire_requirement(self, req: dict) -> None:
        name = str(req.get("name") or "")
        src_rel = req.get("from")
        if not name or not src_rel:
            return
        src = (_REPO_ROOT / src_rel).resolve()
        if not src.is_dir():
            return
        dst = self.req_root / name
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copy2(f, dst / f.name)
        self._fired.add(name)

    def _answer_pending(self) -> None:
        """If a worker question is waiting and no answer is staged, reply from
        the declared answers (faq match, else default)."""
        if self.answer.exists() or not self.questions.is_file():
            return
        text = self.questions.read_text(encoding="utf-8", errors="replace")
        asked = text.count("asks:")
        answered = text.count("**answer:**")
        if asked <= answered or asked <= self._answered:
            return
        # the last question block is the pending one
        last = text.rsplit("asks:", 1)[-1]
        reply = self.answer_default
        for item in self.answer_faq:
            m = str(item.get("match") or "")
            if m and m.lower() in last.lower():
                reply = item.get("reply") or reply
                break
        if not reply:
            return
        self.answer.write_text(reply, encoding="utf-8")
        self._answered = asked
