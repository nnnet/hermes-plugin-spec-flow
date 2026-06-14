"""Working language is configurable (env / workers.language), default
English, and injected into every role's system prompt. A worker once
asked its HITL question in Russian and the English auto-responder missed
it — the root fix is to PIN the language in the prompts."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import llm_backend as lb       # noqa: E402
from harness import role_worker as rw        # noqa: E402


def teardown_function(_fn):
    lb.configure_workers(None)


def test_default_language_is_english(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_LANG", raising=False)
    lb.configure_workers(None)
    assert rw.worker_language() == "English"


def test_env_overrides_language(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_LANG", "French")
    assert rw.worker_language() == "French"


def test_workers_block_sets_language(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_LANG", raising=False)
    lb.configure_workers({"language": "German"})
    assert rw.worker_language() == "German"


def test_env_beats_workers_block(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_LANG", "Spanish")
    lb.configure_workers({"language": "German"})
    assert rw.worker_language() == "Spanish"


def test_directive_appended_to_system(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_LANG", raising=False)
    lb.configure_workers(None)
    out = rw._with_language("BASE SKILL TEXT")
    assert out.startswith("BASE SKILL TEXT")
    assert "Working language" in out and "English" in out


def test_directive_reflects_configured_language(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_LANG", "Italian")
    assert "Italian" in rw._with_language("x")
