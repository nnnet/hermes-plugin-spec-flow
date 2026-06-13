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
           "?", "？", "instead")   # ASCII + full-width (CJK) question mark

# ROBUSTNESS PRINCIPLE: the worker is told to speak the plugin language
# (English), so a question in ANY OTHER language is a robustness probe —
# the plugin must still recognise the known-policy class and not stall.
# We therefore key on LANGUAGE-NEUTRAL ANCHORS only: file/library/symbol
# names, URL shapes, HTTP verbs — code tokens that appear verbatim
# regardless of the surrounding prose. Never a per-language phrase list.

# class 1: touching platform / protected / read-only code.
# Anchors are the literal platform symbols (a German/French/RU question
# about app.py still contains 'app.py').
# only UNAMBIGUOUS platform internals (a bare 'registry' is a common
# domain word — dropped to avoid false positives on business questions)
_PLATFORM_FILE = ("app.py", "dispatcher", "wsgi", "path_info",
                  "exact path matching", "read-only dispatcher")
# modify-intent words across a few languages is a per-language list and
# thus brittle — instead a platform-symbol anchor + a question mark is
# enough (see answer()); these stay only as a soft EN hint.
_MODIFY = ("modify", "edit", "change", "extend", "patch", "alter")

# class 2: an explicit constraint conflicts with the spec. Anchors are
# library/symbol names and tokens that are the SAME in every language.
_CONSTRAINT = ("stdlib", "standard library", "third-party", "third party",
               "no third", "constraint")
_LIB_ANCHOR = ("bcrypt", "import ", "pip install", "requirements.txt",
               "package", "hashlib", "pbkdf2")

# class 3: dynamic path parameters against an exact-match dispatcher.
_ROUTING = ("path parameter", "path param", "{id}", "<id>", "dynamic path",
            "exact path", "exact match", "path_info", "/{", "?id=",
            "/<", "query param", "query string", "query-")
# a routing question almost always carries an HTTP verb next to a path —
# language-neutral, present whatever the prose language
_HTTP_VERB = ("get /", "post /", "put /", "patch /", "delete /")

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
    to defer to the human. Language-AGNOSTIC: keys on neutral code anchors
    so the class is caught whatever language the worker used (a robustness
    probe injects a non-plugin language on purpose). Conservative: silence
    beats a wrong auto-yes."""
    q = (question or "").lower()
    if not _has(q, _ASKING):
        return None
    # routing — a dynamic-path marker OR an HTTP verb next to a path is a
    # strong enough neutral signal on its own.
    if _has(q, _ROUTING) or _has(q, _HTTP_VERB):
        return ("routing", _ROUTING_ANSWER)
    # constraint-vs-spec — a constraint token + a library/symbol anchor,
    # OR a library anchor alongside an explicit 'standard library' token.
    if _has(q, _LIB_ANCHOR) and _has(q, _CONSTRAINT):
        return ("constraint-vs-spec", _CONSTRAINT_ANSWER)
    # platform-readonly — a literal platform symbol in a QUESTION is the
    # neutral anchor (modify-intent words are per-language and unreliable).
    if _has(q, _PLATFORM_FILE):
        return ("platform-readonly", _PLATFORM_ANSWER)
    return None
