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
    """retain/recall contract every provider implements."""

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
