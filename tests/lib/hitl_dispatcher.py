"""Autonomous HITL dispatcher — replays a declarative injection script against a
live run so no human has to babysit it.

The HITL channel (``tests/harness/hitl.py``) is purely file-based: the engine
polls ``<run>/hitl/`` for standing requirements and answers. A human normally
watches ``trace.jsonl`` and, at the right moment, drops the same files by hand
(create ``hitl/requirements/<name>/`` for a late requirement, write
``hitl/answer.md`` to reply to a blocked worker). This dispatcher does exactly
that, driven by the case's ``injections:`` block, on a daemon thread inside the
run process — it writes the very files a human would.

Declared INLINE in the case YAML — the whole injection lives in the scenario,
no sidecar files. A human only states a requirement IN WORDS and answers
clarifying questions; the system builds AND verifies the feature itself::

    injections:
      requirements:
        - name: web_ui
          when: {event: "minimal impl"}
          statement: |                  # → hitl/requirements/web_ui/REQUIREMENT.md
            MINIMAL WEB INTERFACE ...    # the prose a person types; no test handed
      answers:                          # possible human replies, given ONLY when a
        default: "Do the simplest correct thing; don't block."   # worker asks, and
        faq:                            # only the one matching the actual question
          - {match: "port", reply: "Any free localhost port is fine."}

Triggers (``when:``) — first match fires the requirement once:
  * ``integrate_passes: N`` — ≥N ``integrate_verify`` gates reached PASS
  * ``event: <substr>``     — a trace line's action/detail contains <substr>
  * ``start: true``         — fire immediately at run start

The dispatcher never touches the workers' code or the engine; it only writes
into ``<run>/hitl/``, identical to manual operation.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Optional

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
        reqs = list(inj.get("requirements") or [])
        sel = inj.get("select") or {}
        pool = list(inj.get("pool") or [])
        # Randomised EXTRA injections on top of the always-on baseline: each run
        # draws a different subset of `pool` (some OVERLAP a module already built,
        # some are FRESH surfaces) so we exercise late-requirement routing in the
        # general case, not one fixed combination. The choice is persisted to
        # hitl/selection.json so a --resume run replays the SAME set (the tree
        # must match). Baseline `requirements` (e.g. web_ui) stay mandatory —
        # the boot-gate hard-requires /ui.
        if sel.get("enabled") and pool:
            reqs = reqs + self._select_injections(pool, sel)
        self.requirements = reqs
        ans = inj.get("answers") or {}
        self.answer_default = ans.get("default")
        self.answer_faq = list(ans.get("faq") or [])
        self._fired: set[str] = set()
        self._answered = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _select_injections(self, pool: list, sel: dict) -> list:
        """Draw a random subset of the EXTRA-requirement pool for this run.

        Why: robustness in the general case needs a different late-requirement
        mix each run — some OVERLAPPING an existing module (``overlap: true``),
        some FRESH. What: pick ``randint(min, max)`` items, guaranteeing a
        mix of both classes when the count allows; persist the chosen names to
        ``hitl/selection.json`` so a --resume run replays the same set; preserve
        the pool's declaration order (sequencing between requirements matters).
        Entropy comes from ``SPEC_FLOW_INJECT_SEED`` when set (reproducible),
        else from ``os.urandom`` (a fresh combination every run).
        Test: a 4-item pool with min=max=2 yields exactly 2 names, and a second
        Dispatcher over the same run dir reads back the identical pair.
        """
        by_name = {str(r.get("name")): r for r in pool if r.get("name")}
        sel_file = self.hitl / "selection.json"
        if sel_file.is_file():                       # resume: replay the choice
            try:
                names = json.loads(sel_file.read_text(encoding="utf-8"))
                chosen = [by_name[n] for n in names if n in by_name]
                if chosen:
                    return chosen
            except (OSError, ValueError):
                pass
        seed = os.environ.get("SPEC_FLOW_INJECT_SEED")
        rnd = random.Random(int(seed) if (seed or "").lstrip("-").isdigit()
                            else os.urandom(16))
        lo = max(0, int(sel.get("min", 1)))
        hi = min(int(sel.get("max", len(pool))), len(pool))
        hi = max(hi, lo)
        k = rnd.randint(lo, hi)
        overlap = [r for r in pool if r.get("overlap")]
        fresh = [r for r in pool if not r.get("overlap")]
        rnd.shuffle(overlap)
        rnd.shuffle(fresh)
        chosen: list = []
        if k >= 2 and overlap and fresh:             # guarantee a mix
            chosen += [overlap.pop(), fresh.pop()]
        rest = overlap + fresh
        rnd.shuffle(rest)
        while len(chosen) < k and rest:
            chosen.append(rest.pop())
        order = {r.get("name"): i for i, r in enumerate(pool)}
        chosen.sort(key=lambda r: order.get(r.get("name"), 0))
        try:
            self.hitl.mkdir(parents=True, exist_ok=True)
            sel_file.write_text(
                json.dumps([r.get("name") for r in chosen]), encoding="utf-8")
        except OSError:
            pass
        return chosen

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
        passes, actions, done = self._scan_trace()
        for req in self.requirements:
            name = str(req.get("name") or "")
            if not name or name in self._fired:
                continue
            when = req.get("when") or {}
            if "integrate_passes" in when and passes >= int(when["integrate_passes"]):
                self._fire_requirement(req)
            elif when.get("event") and any(when["event"] in a for a in actions):
                self._fire_requirement(req)
            elif when.get("after_node_done") and \
                    str(when["after_node_done"]) in done:
                # deterministic sequencing: fire only once the named node has
                # reached DONE — lets a second injection land AFTER a specific
                # earlier one is fully built (e.g. nice_ui after web_ui), so it
                # can react to the existing artifact instead of racing it
                self._fire_requirement(req)
        self._answer_pending()

    # -- trace reading ----------------------------------------------------
    def _scan_trace(self) -> tuple[int, list[str], set]:
        """Return (#integrate_verify PASS, [action+detail strings],
        {task names that reached DONE})."""
        if not self.trace.is_file():
            return 0, [], set()
        passes = 0
        actions: list[str] = []
        done: set = set()
        for ln in self.trace.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("gate") == "integrate_verify" and str(e.get("verdict")).upper() == "PASS":
                passes += 1
            if str(e.get("action", "")).startswith("to_done") or \
                    "state=DONE" in str(e.get("detail", "")):
                if e.get("task"):
                    done.add(str(e.get("task")))
            actions.append(f"{e.get('action', '')} {e.get('detail', '')}")
        return passes, actions, done

    # -- actions ----------------------------------------------------------
    def _fire_requirement(self, req: dict) -> None:
        """Materialise the late requirement from its inline prose: the
        ``statement:`` (what a human types in the HITL box) becomes
        REQUIREMENT.md, read by the engine and shown to the decomposer. The
        human hands NO test — building the feature AND proving it works is the
        system's own job (the worker writes the leaf and its test)."""
        name = str(req.get("name") or "")
        statement = req.get("statement")
        if not name or not statement:
            return
        dst = self.req_root / name
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "REQUIREMENT.md").write_text(str(statement), encoding="utf-8")
        self._fired.add(name)

    def _answer_pending(self) -> None:
        """If a worker question is waiting and no answer is staged, reply from
        the declared answers (faq match, else default)."""
        if self.answer.exists() or not self.questions.is_file():
            return
        text = self.questions.read_text(encoding="utf-8", errors="replace")
        # a worker question is a markdown header "## [HH:MM:SS] role @ node"
        # (hitl.py ask() writes that to questions.md — NOT the literal "asks:",
        # which only appears on the interactive TTY prompt). Counting "asks:"
        # meant questions were never detected, so the worker burned the full
        # ASK_TIMEOUT (~3 min) waiting for an answer that never came.
        asked = text.count("## ")
        if asked <= self._answered:
            return
        # the last question block is the pending one
        last = text.rsplit("## ", 1)[-1]
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
