"""Auto-responder: known-policy questions answered instantly, novel
ones deferred to the human. Cases are the REAL questions v19/v20 workers
asked (each burned a 300s window)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import auto_responder as ar  # noqa: E402


# ─── the three recurring classes (verbatim from live runs) ────────────

def test_routing_path_param_question_is_auto_answered():
    q = ("The spec requires GET /products/{id} with a path parameter. "
         "The handler interface handler(payload, query) doesn't receive "
         "the path, and app.py uses exact path matching. How should I "
         "implement this? Should I use GET /products?id=... instead?")
    res = ar.answer(q)
    assert res is not None and res[0] == "routing"
    assert "query string" in res[1] and "do not modify" in res[1].lower()


def test_stdlib_constraint_vs_spec_is_auto_answered():
    q = ("Spec requires bcrypt (cost 12) for password hashing, but the "
         "platform constraint states 'Standard library only — no "
         "third-party imports'. Should I use bcrypt or pbkdf2_hmac?")
    res = ar.answer(q)
    assert res is not None and res[0] == "constraint-vs-spec"
    assert "constraint" in res[1].lower()


def test_modify_platform_file_is_auto_answered():
    q = ("Should I extend app.py to support pattern matching, or modify "
         "the platform dispatcher to add a catch-all route?")
    res = ar.answer(q)
    assert res is not None and res[0] in ("routing", "platform-readonly")
    assert "read-only" in res[1].lower() or "do not modify" in res[1].lower()


# ─── conservative: novel questions reach the human ────────────────────

def test_business_question_is_deferred():
    q = ("Should the seller payout threshold be $50 or $100 before a "
         "transfer is triggered? This affects the fee model.")
    assert ar.answer(q) is None


def test_statement_without_a_question_is_deferred():
    # a mere mention of a read-only platform, no actual ask
    assert ar.answer("The platform app.py is read-only and stable.") is None


def test_empty_question_is_deferred():
    assert ar.answer("") is None
    assert ar.answer(None) is None


# ─── channel integration: auto-answer returns without the wait ────────

def test_channel_auto_answers_without_human_window(tmp_path, monkeypatch):
    from harness import hitl
    ch = hitl.HumanChannel(tmp_path / "hitl")
    # if the human window were entered this would hang; a fast return
    # proves the auto-path was taken
    monkeypatch.setattr(hitl.sys.stdin, "isatty", lambda: False)
    ans = ch.ask("implementer", "user_login",
                 "The constraint is stdlib-only but the spec requires "
                 "bcrypt — which wins?")
    assert ans and "constraint" in ans.lower()
    # the transcript records the auto answer with its class
    qfile = (ch.questions).read_text()
    assert "answer (auto/constraint-vs-spec)" in qfile


def test_channel_defers_novel_question(tmp_path, monkeypatch):
    from harness import hitl
    ch = hitl.HumanChannel(tmp_path / "hitl")
    monkeypatch.setattr(hitl.sys.stdin, "isatty", lambda: False)
    # background mode with no answer.md and a near-zero wait -> the novel
    # question is DEFERRED (no auto answer), the worker proceeds on its own
    monkeypatch.setattr(hitl, "ASK_TIMEOUT", 0.05)
    ans = ch.ask("decomposer", "payout_policy",
                 "Should the payout threshold be $50 or $100?")
    assert not ans
    assert "answer (auto/" not in ch.questions.read_text()


def test_russian_routing_question_is_auto_answered():
    # LIVE gap (v22): a worker asked the routing question in Russian and
    # the English-only matcher missed it — it waited the full window.
    # Language-neutral code tokens (<id>, ?id=, GET /) must catch it.
    q = ("Спецификация требует GET /categories/<id> путь, но маршрутизатор"
         " не поддерживает динамические параметры пути. Использовать GET"
         " /categories с query-параметром (например ?id=123) или есть"
         " другой механизм?")
    res = ar.answer(q)
    assert res is not None and res[0] == "routing"


def test_http_verb_with_path_alone_triggers_routing():
    q = "Should the endpoint be POST /orders/{id}/cancel or a query param?"
    res = ar.answer(q)
    assert res is not None and res[0] == "routing"


def test_plain_business_question_still_deferred_after_neutral_tokens():
    # adding neutral tokens must NOT make it fire on non-routing asks
    q = "Should the payout threshold be 50 or 100 dollars before transfer?"
    assert ar.answer(q) is None


# ─── robustness: ANY non-plugin language must still be caught ──────────

def test_german_routing_question_caught():
    q = ("Die Spezifikation verlangt GET /categories/{id}, aber der "
         "Dispatcher unterstützt keine dynamischen Pfade. Soll ich einen "
         "query-Parameter verwenden?")
    res = ar.answer(q)
    assert res is not None and res[0] == "routing"


def test_french_stdlib_constraint_caught():
    q = ("La spec demande bcrypt mais la contrainte est 'standard library "
         "only'. Dois-je utiliser bcrypt ou hashlib?")
    res = ar.answer(q)
    assert res is not None and res[0] == "constraint-vs-spec"


def test_chinese_platform_question_caught():
    q = "我应该修改 app.py 来支持动态路由吗？"  # may I modify app.py ...?
    res = ar.answer(q)
    assert res is not None and res[0] in ("routing", "platform-readonly")


def test_business_question_in_any_language_deferred():
    # neutral anchors must NOT fire on a plain domain question
    assert ar.answer("Soll die Auszahlungsschwelle 50 oder 100 sein?") is None
    assert ar.answer("¿El umbral de pago debe ser 50 o 100?") is None
