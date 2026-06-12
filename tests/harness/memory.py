"""Worker/project memory — plan part 6 (seasoning, not foundation).

Banks: ``specflow-role-<role>`` accumulates a ROLE's craft across runs
(how this codebase shapes specs, which repair patterns worked);
``specflow-project-<case>`` accumulates one case's decisions (chosen
contracts, resolved open questions, operator answers).

Two providers behind one interface:
  * ``HindsightMemory`` — the REAL store: the Hindsight service from the
    Hermes docker-compose stack (default http://127.0.0.1:8888, REST
    /v1/default/banks/<bank>/memories[+/recall], banks auto-create).
  * ``FakeMemory``      — in-process stand-in for OFFLINE tests: the
    suite must stay green without the Hermes stack or network.

Memory failures NEVER kill a run: retain returns False, recall returns
[] — a worker without memory is a worker as before, not a crash.

Test: tests/test_memory_provider.py.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_URL = os.environ.get("SPEC_FLOW_HINDSIGHT_URL",
                             "http://127.0.0.1:8888")
RECALL_LIMIT = 3            # entries per recall block
RECALL_BUDGET_CHARS = 1200  # hard cap on what reaches a prompt


def role_bank(role: str) -> str:
    return f"specflow-role-{role}"


def project_bank(case: str) -> str:
    return "specflow-project-" + re.sub(r"\W+", "-", case).strip("-").lower()


class MemoryProvider:
    """retain/recall/clear contract every provider implements."""

    def clear(self, bank: str) -> bool:
        raise NotImplementedError

    def retain(self, bank: str, content: str, *,
               context: str = "", tags: Optional[list[str]] = None) -> bool:
        raise NotImplementedError

    def recall(self, bank: str, query: str, *, limit: int = RECALL_LIMIT,
               budget_chars: int = RECALL_BUDGET_CHARS) -> list[str]:
        raise NotImplementedError

    def recall_block(self, bank: str, query: str, **kw) -> str:
        """Prompt-ready block ('' when nothing relevant) — memory is
        seasoning: an empty block must cost the prompt nothing."""
        hits = self.recall(bank, query, **kw)
        if not hits:
            return ""
        return ("\n\nRELEVANT EXPERIENCE (from earlier runs; advisory,"
                " the spec and constitution always win):\n"
                + "\n".join(f"- {h}" for h in hits))


class FakeMemory(MemoryProvider):
    """In-process provider for offline tests: keyword-overlap recall,
    insertion-ordered tie-break (newest first)."""

    def __init__(self):
        self._banks: dict[str, list[str]] = {}

    def retain(self, bank, content, *, context="", tags=None) -> bool:
        self._banks.setdefault(bank, []).append(str(content))
        return True

    def clear(self, bank) -> bool:
        self._banks.pop(bank, None)
        return True

    def recall(self, bank, query, *, limit=RECALL_LIMIT,
               budget_chars=RECALL_BUDGET_CHARS) -> list[str]:
        words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
        scored = []
        for pos, item in enumerate(self._banks.get(bank, [])):
            got = {w for w in re.findall(r"\w+", item.lower()) if len(w) > 2}
            score = len(words & got)
            if score:
                scored.append((-score, -pos, item))
        out, spent = [], 0
        for _, _, item in sorted(scored)[:limit]:
            if spent + len(item) > budget_chars:
                break
            out.append(item)
            spent += len(item)
        return out


class HindsightMemory(MemoryProvider):
    """REST adapter for the Hindsight service of the Hermes stack."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # separated for offline test stubbing. The opener carries NO proxies:
    # the service lives on localhost and a host http_proxy env var would
    # otherwise swallow every call (urllib honors proxies, curl --noproxy
    # was the working reference)
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}))

    def _http_post(self, url: str, payload: dict) -> tuple[int, str]:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _http_delete(self, url: str) -> int:
        req = urllib.request.Request(url, method="DELETE")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def clear(self, bank) -> bool:
        try:
            status = self._http_delete(
                f"{self.base_url}/v1/default/banks/{bank}/memories")
            return status in (200, 202, 204, 404)
        except Exception:            # noqa: BLE001 — memory never kills a run
            return False

    def retain(self, bank, content, *, context="", tags=None) -> bool:
        # strip markdown decoration — the extractor returns 0 facts on raw
        # markdown (empirical, see docs/hindsight-guide.md)
        plain = re.sub(r"[*`#>]+", "", str(content)).strip()
        item: dict[str, Any] = {"content": plain}
        if context:
            item["context"] = context
        if tags:
            item["tags"] = list(tags)
        try:
            status, _ = self._http_post(
                f"{self.base_url}/v1/default/banks/{bank}/memories",
                {"items": [item], "async": True})
            return status in (200, 201, 202)
        except Exception:           # noqa: BLE001 — memory must not kill a run
            return False

    def recall(self, bank, query, *, limit=RECALL_LIMIT,
               budget_chars=RECALL_BUDGET_CHARS) -> list[str]:
        try:
            status, body = self._http_post(
                f"{self.base_url}/v1/default/banks/{bank}/memories/recall",
                {"query": query, "max_tokens": max(64, budget_chars // 3)})
            if status != 200:
                return []
            results = (json.loads(body) or {}).get("results") or []
        except Exception:            # noqa: BLE001
            return []
        out, spent = [], 0
        for r in results[:limit]:
            text = str(r.get("text") or r.get("content") or "").strip()
            if not text or spent + len(text) > budget_chars:
                continue
            out.append(text)
            spent += len(text)
        return out


def make_provider(cfg: Optional[dict]) -> Optional[MemoryProvider]:
    """Provider from the case YAML ``memory:`` block; None when disabled.

    memory: {provider: hindsight, url: "http://127.0.0.1:8888"}
    memory: {provider: fake}      # offline tests
    """
    if not cfg or not cfg.get("provider"):
        return None
    kind = str(cfg["provider"]).lower()
    if kind == "fake":
        return FakeMemory()
    if kind == "hindsight":
        return HindsightMemory(base_url=cfg.get("url", DEFAULT_URL))
    raise ValueError(f"unknown memory provider {kind!r} (fake|hindsight)")


ROLE_MODES = ("accumulate", "readonly", "fresh", "off")
PROJECT_MODES = ("fresh", "resume", "readonly", "off")
_KNOWN_ROLES = ("decomposer", "reviewer", "implementer", "verifier",
                "researcher")


class MemoryManager:
    """Mode-aware front for the two memory tiers.

    ROLE banks carry a role's craft ACROSS runs; the PROJECT bank carries
    one case's decisions. The case YAML picks what happens to each at run
    start and what the run may do to them:

      memory:
        provider: hindsight | fake
        roles:   {mode: accumulate|readonly|fresh|off}   # default accumulate
        project: {mode: fresh|resume|readonly|off}       # default fresh
        recall_budget_chars: 1200

    roles.mode   — accumulate: read+write, craft grows run over run;
                   readonly: recall only (a noisy experiment must not
                   pollute the banks); fresh: clear role banks at start,
                   then read+write; off: no role memory.
    project.mode — fresh: a NEW project starts with a clean bank (the
                   default — stale decisions of a previous attempt are
                   poison); resume: continuation of the SAME project,
                   keep everything; readonly: recall only; off.
    """

    def __init__(self, provider: MemoryProvider, case_name: str,
                 roles_mode: str = "accumulate",
                 project_mode: str = "fresh",
                 budget_chars: int = RECALL_BUDGET_CHARS):
        if roles_mode not in ROLE_MODES:
            raise ValueError(
                f"memory.roles.mode must be one of {ROLE_MODES}")
        if project_mode not in PROJECT_MODES:
            raise ValueError(
                f"memory.project.mode must be one of {PROJECT_MODES}")
        self.provider = provider
        self.case = case_name
        self.roles_mode = roles_mode
        self.project_mode = project_mode
        self.budget = budget_chars

    def setup(self) -> dict:
        """Apply the start-of-run modes; returns what was cleared."""
        cleared = {"roles": [], "project": False}
        if self.roles_mode == "fresh":
            for role in _KNOWN_ROLES:
                if self.provider.clear(role_bank(role)):
                    cleared["roles"].append(role)
        if self.project_mode == "fresh":
            cleared["project"] = self.provider.clear(
                project_bank(self.case))
        return cleared

    # -- role tier ---------------------------------------------------------
    def recall_role(self, role: str, query: str) -> str:
        if self.roles_mode == "off":
            return ""
        return self.provider.recall_block(role_bank(role), query,
                                          budget_chars=self.budget)

    def retain_role(self, role: str, content: str, *,
                    context: str = "", tags=None) -> bool:
        if self.roles_mode in ("off", "readonly"):
            return False
        return self.provider.retain(role_bank(role), content,
                                    context=context, tags=tags)

    # -- project tier --------------------------------------------------------
    def recall_project(self, query: str) -> str:
        if self.project_mode == "off":
            return ""
        return self.provider.recall_block(project_bank(self.case), query,
                                          budget_chars=self.budget)

    def retain_project(self, content: str, *, context: str = "",
                       tags=None) -> bool:
        if self.project_mode in ("off", "readonly"):
            return False
        return self.provider.retain(project_bank(self.case), content,
                                    context=context, tags=tags)


# the run-wide manager — set by the runner from the case YAML; workers
# consult it through the helpers below (None = memory disabled, all
# helpers degrade to no-ops: a worker without memory is a worker as before)
MANAGER: Optional[MemoryManager] = None


def configure(cfg: Optional[dict], case_name: str) -> Optional[MemoryManager]:
    """Build + install the run-wide manager from the case YAML block;
    clears banks per the start-of-run modes. None when memory is off."""
    global MANAGER
    provider = make_provider(cfg)
    if provider is None:
        MANAGER = None
        return None
    cfg = cfg or {}
    MANAGER = MemoryManager(
        provider, case_name,
        roles_mode=str((cfg.get("roles") or {}).get("mode", "accumulate")),
        project_mode=str((cfg.get("project") or {}).get("mode", "fresh")),
        budget_chars=int(cfg.get("recall_budget_chars",
                                 RECALL_BUDGET_CHARS)))
    MANAGER.setup()
    return MANAGER


def recall_block_for(role: str, query: str) -> str:
    """Worker-facing: role craft + project decisions in one block."""
    if MANAGER is None:
        return ""
    return MANAGER.recall_role(role, query) + MANAGER.recall_project(query)


def retain_role(role: str, content: str, **kw) -> bool:
    return MANAGER.retain_role(role, content, **kw) if MANAGER else False


def retain_project(content: str, **kw) -> bool:
    return MANAGER.retain_project(content, **kw) if MANAGER else False
