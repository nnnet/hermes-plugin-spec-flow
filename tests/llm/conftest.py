"""Fixtures that point the REAL backend at a REAL local OpenAI-compatible server.

No monkeypatch: the fixture sets the backend's CONFIG (BASE_URL/API_KEY/retries)
to a localhost server and restores it afterwards. The harness's real HTTP path
runs unaltered against it — only the upstream replies are scripted.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from harness import llm_backend as lb          # noqa: E402
from harness_fakeapi import FakeOpenAI, ok      # noqa: E402,F401  (ok re-exported)


@pytest.fixture
def fake_openai():
    """Factory: ``fake_openai(script, retries=3, backoff=0.01)`` starts a real
    localhost OpenAI endpoint serving ``script`` (a list of ``(status, body)``)
    and points the backend at it. Returns the server (inspect ``.requests``).
    Config is saved and restored on teardown — backoff is tiny so the real
    retry sleeps don't slow the test."""
    started: list = []
    keys = ("BASE_URL", "API_KEY", "RETRIES", "BACKOFF", "BACKEND")
    saved = {k: getattr(lb, k) for k in keys}

    def _make(script, *, retries: int = 3, backoff: float = 0.01) -> FakeOpenAI:
        srv = FakeOpenAI(script).__enter__()
        started.append(srv)
        lb.BASE_URL = srv.base_url
        lb.API_KEY = "test-key"
        lb.RETRIES = retries
        lb.BACKOFF = backoff
        lb.BACKEND = "openai"
        return srv

    yield _make
    for k, v in saved.items():
        setattr(lb, k, v)
    for srv in started:
        srv.__exit__(None, None, None)
