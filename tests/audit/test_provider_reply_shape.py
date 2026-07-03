"""Audit rule S10.28 (v158): a provider returning message.content as a LIST of
content parts must never crash the call path.

v158 root of the OPEN doctor cause `about_page:empty_delta`: the delta gate
was RIGHT (src/core.py truly gained no /about handler) — the leaf delivered
NOTHING because the openai backend assumed ``message.content`` is a string;
a provider answered with the content-parts shape
(``[{"type": "text", "text": ...}]``), ``text.strip()`` raised
AttributeError, which escaped BOTH the narrow shape-except
(KeyError/IndexError/JSONDecodeError) AND ask()'s RuntimeError-only chain
except — the orchestra coder AND tester steps died with
``'list' object has no attribute 'strip'`` (llm-log events 155/160), the
about_page amend wrote zero files, and the doctor cause stayed open for the
whole run.

Contract: the parse seam normalizes the message to plain text (string, list
of parts, nested part dicts); ANY residual shape surprise degrades to the
'bad response shape' retry/fallback — never an uncaught crash.

REAL seam per project rule: a real local HTTP server (FakeOpenAI) drives the
backend's genuine request/parse/retry path — no monkeypatch on the LLM path.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from harness_fakeapi import ok  # noqa: E402


def _parts_body(parts) -> str:
    return json.dumps({"choices": [{"message": {"content": parts}}]})


def test_list_shaped_content_parts_return_joined_text(fake_openai):
    from harness import llm_backend as lb
    fake_openai([(200, _parts_body([
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"}]))])
    out = lb.ask("hi", model="openrouter/a:free", role="implementer", step="")
    assert out == "hello world", (
        "content-parts replies must be normalized to text, not crash "
        "(v158: AttributeError killed the orchestra coder/tester steps and "
        "about_page delivered nothing)")


def test_weird_content_shape_degrades_to_retry_never_crashes(fake_openai):
    # a dict-shaped content (no usable text) is a BAD SHAPE, not a crash:
    # the attempt degrades and the next reply answers
    from harness import llm_backend as lb
    fake_openai([(200, _parts_body({"foo": 1})), (200, ok("recovered"))],
                retries=2)
    out = lb.ask("hi", model="openrouter/a:free", role="implementer", step="")
    assert out == "recovered", (
        "an unrecognizable content shape must roll to the next attempt, "
        "never raise out of ask()")


def test_plain_string_content_still_works(fake_openai):
    from harness import llm_backend as lb
    fake_openai([(200, ok("plain"))])
    assert lb.ask("hi", model="openrouter/a:free",
                  role="implementer", step="") == "plain"
