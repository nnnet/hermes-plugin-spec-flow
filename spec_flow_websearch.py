"""web_search remedy: when a stronger model still cannot resolve an error, the
doctor's ladder may consult the internet/docs before escalating to a human.

Network- and key-optional by design: with no backend configured (no API key, or
the host is offline/sandboxed) it returns a structured EMPTY result instead of
raising, so a run never breaks because search was unavailable — the ladder simply
advances to the next rung (ask_human). Backend is chosen by env:
  SPEC_FLOW_SEARCH_BACKEND = tavily | none      (default: auto -> tavily if key)
  TAVILY_API_KEY                                (when backend=tavily)
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


def _backend() -> str:
    b = os.environ.get("SPEC_FLOW_SEARCH_BACKEND", "").strip().lower()
    if b:
        return b
    return "tavily" if os.environ.get("TAVILY_API_KEY") else "none"


def _tavily(query: str, k: int, timeout: float) -> list:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    payload = json.dumps({"api_key": key, "query": query,
                          "max_results": k}).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode())
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")[:500]}
            for r in (data.get("results") or [])[:k]]


def search(query: str, *, k: int = 3, timeout: float = 10.0) -> dict:
    """Return {"query","backend","results":[{title,url,snippet}],"available":bool,
    "error":str}. Never raises — failures degrade to available=False."""
    backend = _backend()
    if backend == "none" or not str(query or "").strip():
        return {"query": query, "backend": "none", "results": [],
                "available": False, "error": ""}
    try:
        if backend == "tavily":
            res = _tavily(query, k, timeout)
            return {"query": query, "backend": "tavily", "results": res,
                    "available": True, "error": ""}
        return {"query": query, "backend": backend, "results": [],
                "available": False, "error": f"unknown backend {backend}"}
    except Exception as exc:  # noqa: BLE001 — search must never break a run
        return {"query": query, "backend": backend, "results": [],
                "available": False, "error": str(exc)[:160]}


def summarize(result: dict, limit: int = 3) -> str:
    """A compact text block to feed back to a worker as research context."""
    rows = (result or {}).get("results") or []
    if not rows:
        return "(web_search: no results available)"
    return "\n".join(f"- {r['title']}: {r['snippet'][:200]} <{r['url']}>"
                     for r in rows[:limit])
