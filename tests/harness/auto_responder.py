"""Auto-responder for known-policy worker questions.

Live observation (v19, v20): workers burn the full 300s HITL window on
questions whose answer is FIXED POLICY, asked over and over —

  * 'the dispatcher in app.py only does exact paths, may I modify it?'
  * 'the spec says bcrypt but the constraint is stdlib-only, which wins?'
  * 'may I edit <a protected / platform file> to satisfy my spec?'

None of these need a human: the answer is the project's own
constitution. This module recognises the CLASS of question by intent
(English keyword sets, never a brittle phrase list) and returns the
deterministic policy answer instantly. Each firing is logged
(event=auto_answer) so its effect on run time is visible.

Conservative by design: it answers ONLY the classes it is sure of and
stays silent otherwise, so a genuinely novel question still reaches the
human window. It never says 'yes, break the rule' — every answer keeps
the worker inside the platform contract.
"""
from __future__ import annotations

from typing import Optional

# intent is detected by co-occurrence of a TOPIC with an ASKING cue, so
# a mere mention ('the platform is read-only') without a question does
# not trigger — the worker must actually be asking permission/choice
_ASKING = ("should i", "may i", "can i", "is it ok", "which wins",
           "do i", "am i allowed", "how should i", "or is there",
           "?", "instead")

# class 1: touching platform / protected / read-only code
_PLATFORM_FILE = ("app.py", "dispatcher", "registry", "platform",
                  "read-only", "read only", "protected file")
_MODIFY = ("modify", "edit", "change", "extend", "patch", "alter")

# class 2: an explicit constraint conflicts with the spec
_CONSTRAINT = ("constraint", "stdlib", "standard library",
               "third-party", "third party", "no third", "forbid")
_SPEC_CONFLICT = ("spec", "requirement", "bcrypt", "deviat", "exception",
                  "wins", "violat")

# class 3: dynamic path parameters against an exact-match dispatcher
_ROUTING = ("path parameter", "path param", "{id}", "dynamic path",
            "exact path", "exact match", "path_info", "/{")

_PLATFORM_ANSWER = (
    "Platform and protected files are READ-ONLY — do not modify them. "
    "Work entirely within your own module's public interface "
    "(functions taking (payload, query)). If the platform lacks a "
    "feature you want, adapt your design to what it offers; do not "
    "change platform code."
)
_CONSTRAINT_ANSWER = (
    "The platform constraint WINS over the spec. Stay strictly within "
    "the constraint (e.g. standard library only — no third-party "
    "imports) and update your spec's acceptance wording to match. Never "
    "break a stated constraint to satisfy a spec suggestion."
)
_ROUTING_ANSWER = (
    "Do NOT modify the platform dispatcher. Use exact paths with the id "
    "in the query string (e.g. GET /products?id=...). Keep your "
    "handler's (payload, query) interface and read the id from query. "
    "Make your tests and spec acceptance wording match the "
    "query-parameter route."
)


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def answer(question: str) -> Optional[tuple[str, str]]:
    """Return (policy_class, answer) for a known-policy question, or None
    to defer to the human. Conservative: silence beats a wrong auto-yes."""
    q = (question or "").lower()
    if not _has(q, _ASKING):
        return None
    # routing is the most specific platform class — check it first
    if _has(q, _ROUTING) and (_has(q, _MODIFY) or _has(q, _PLATFORM_FILE)
                              or "query" in q):
        return ("routing", _ROUTING_ANSWER)
    if _has(q, _CONSTRAINT) and _has(q, _SPEC_CONFLICT):
        return ("constraint-vs-spec", _CONSTRAINT_ANSWER)
    if _has(q, _PLATFORM_FILE) and _has(q, _MODIFY):
        return ("platform-readonly", _PLATFORM_ANSWER)
    return None
