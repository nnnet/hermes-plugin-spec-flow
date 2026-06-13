"""Worker specialty resolution (#10 / П13).

A node's SPECIALTY routes its implementer to a better-fit model chain
(workers.<role>.specialties.<name>). It can be set three ways, highest
priority first:

  1. EXPLICIT on the node — the decomposer (or a human) put ``specialty`` on
     the child, OR the case pins one project-wide (``default_specialty``).
  2. AUTO from the node — inferred from its title + spec text via neutral
     keyword anchors (frontend / database / api / auth / payments / ...).
  3. none — the role's normal chain is used.

The auto map is intentionally English-anchored and semantic (a node about
"HTML page" → frontend, "schema/migration" → database), so it works
regardless of the spec's natural language. Test: tests/test_specialty.py.
"""
from __future__ import annotations

import re

# specialty → ordered keyword anchors (checked as whole-word-ish substrings).
# Order matters only for documentation; matching scores each specialty.
_ANCHORS = {
    "frontend": ["html", "css", "page", "ui ", "template", "render",
                 "browser", "form", "wsgi", "http response", "view"],
    "database": ["schema", "migration", "sql", "query", "table", "index",
                 "persist", "storage", "orm", "repository", "db "],
    "api": ["endpoint", "route", "rest", "request", "response", "handler",
            "payload", "http", "json api", "/api", "get /", "post /"],
    "auth": ["auth", "login", "token", "session", "password", "oauth",
             "permission", "consent", "credential"],
    "payments": ["payment", "payout", "invoice", "charge", "refund",
                 "transaction", "billing", "wallet"],
    "messaging": ["queue", "event", "publish", "subscribe", "webhook",
                  "notification", "async", "broker"],
}


def infer_specialty(title: str, spec: str = "") -> str:
    """Best-matching specialty for a node from its title + spec text, or ''
    when nothing scores. Title hits weigh double (a node's name is the
    strongest signal)."""
    t = f" {title.lower()} "
    s = f" {spec.lower()} "
    best, best_score = "", 0
    for name, anchors in _ANCHORS.items():
        score = 0
        for a in anchors:
            if a in t:
                score += 2
            if a in s:
                score += 1
        if score > best_score:
            best, best_score = name, score
    return best


def resolve_specialty(node: dict, project: dict, available: set,
                      auto: bool) -> str:
    """Pick the specialty for a node, honoring config + auto-inference and
    restricting to specialties the role actually declares (``available``).

      * an explicit node['specialty'] wins (if available);
      * else the project default_specialty (if available);
      * else, when ``auto``, the inferred one (if available);
      * else ''.
    """
    def _ok(name: str) -> str:
        return name if name and (not available or name in available) else ""

    explicit = _ok(str(node.get("specialty", "") or ""))
    if explicit:
        return explicit
    pinned = _ok(str((project or {}).get("default_specialty", "") or ""))
    if pinned:
        return pinned
    if auto:
        return _ok(infer_specialty(node.get("title", ""),
                                   node.get("spec_markdown", "")))
    return ""
