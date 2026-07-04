"""Audit rules S14.1-S14.4: the scenario runner is THE interface oracle.

Node B1 (plan 2026-07-04T00-45): the IR's first-class G-W-T scenarios stop
being data-at-rest and become the ONE executable judgement of the interface.
`spec_scenarios.run_scenarios(ir, wsgi_app=...)` drives every scenario
end-to-end through the WSGI surface as a black box: given.env applied
(engine-owned deterministic value factory), given.state replayed, when
performed, then judged (status, media, body_check equals|contains|
json_subset).

Contract pinned here:
  * S14.1 — failures are ATTRIBUTABLE (P4): requirement id, owning node,
    the step, expected vs got as plain JSON-safe strings. Named red case:
    v164's media drift caught BEHAVIOURALLY — the LIVE response medium of
    GET /about (application/json) contradicts then.media (text/html).
    Phase A (S13.5) reds the IR-level disagreement; the runner reds the
    RUNNING product even when the IR is internally consistent.
  * S14.2 — the runner REFUSES an invalid IR (v149's status guess is an IR
    validation error): no request is ever issued against an IR that failed
    `spec_ir.validate_ir` — refusing beats "helpfully" running it.
  * S14.3 — a when.body ABSENT while the route's requestBody has required
    fields is a NAMED incompleteness finding; the scenario is NOT executed
    and NOT counted as passed. The runner never invents a value (no random
    generation, no defaults).
  * S14.4 — env value factory: deterministic and engine-owned. A var whose
    RULE mentions a path/file/db gets a fresh temp path under the run
    workspace; same name -> same value within one run; a new run gets a
    fresh path. Behaviour derives from the IR env entry's rule text, never
    from product-specific name literals.

Deterministic: in-process WSGI callables, no LLM, no network.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_scenarios  # noqa: E402

IR_FORMAT = "spec-flow ir v1"


# --------------------------------------------------------------------------
# tiny IR + WSGI fixtures
# --------------------------------------------------------------------------

def _op(status, media=None, required=None):
    """One OpenAPI operation with a single declared success response."""
    resp = {"description": "contracted success response"}
    if media:
        resp["content"] = {media: {"schema": {}}}
    op = {"responses": {str(status): resp}}
    if required is not None:
        op["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {f: {} for f in required},
                "required": list(required),
                "additionalProperties": False}}}}
    return op


def _openapi(paths):
    return {"openapi": "3.1.0",
            "info": {"title": "t", "version": "1"}, "paths": paths}


def _ir(nodes):
    return {"format": IR_FORMAT, "product": {}, "nodes": nodes}


def _serve(status, ctype, body):
    """A WSGI app answering every request with one fixed response."""
    def app(environ, start_response):
        start_response(status, [("Content-Type", ctype)])
        return [body]
    return app


class _Spy:
    """Wrap a WSGI app and record every (method, path) it actually served."""

    def __init__(self, app):
        self.app, self.calls = app, []

    def __call__(self, environ, start_response):
        self.calls.append((environ.get("REQUEST_METHOD"),
                           environ.get("PATH_INFO")))
        return self.app(environ, start_response)


def _about_ir():
    """One node owning GET /about contracted text/html, with its scenario."""
    return _ir({"about_page": {
        "openapi": _openapi({"/about": {"get": _op(200, "text/html")}}),
        "scenarios": [{
            "requirement": "about_page",
            "when": {"method": "GET", "path": "/about"},
            "then": {"status": 200, "media": "text/html"}}]}})


# --------------------------------------------------------------------------
# S14.1 — the runner is the oracle; failures attributable; v164 behavioural
# --------------------------------------------------------------------------

def test_v164_media_drift_is_a_named_behavioural_failure():
    # the IR is internally CONSISTENT (Phase A validation is green) — only
    # the LIVE product drifts: /about answers JSON against contracted HTML
    app = _serve("200 OK", "application/json",
                 b'{"name": "notes-service", "version": "1.0"}')
    res = spec_scenarios.run_scenarios(_about_ir(), wsgi_app=app)
    assert res["ok"] is False
    assert res["failures"], "a live media drift must be a scenario failure"
    f = res["failures"][0]
    assert f["requirement"] == "about_page"
    assert f["node"] == "about_page"
    assert "GET /about" in f["step"]
    assert "text/html" in f["expected"]
    assert "application/json" in f["got"], (
        "the failure must carry the LIVE medium, v164 class: %r" % f)
    # JSON-safe plain strings (P4: the finding travels through journals)
    json.dumps(res)


def test_conforming_app_is_green():
    app = _serve("200 OK", "text/html", b"<html><body>about</body></html>")
    res = spec_scenarios.run_scenarios(_about_ir(), wsgi_app=app)
    assert res["ok"] is True, res
    assert res["failures"] == []
    assert res["passed"] >= 1, "a judged-green scenario must be counted"


def test_wrong_status_is_a_named_failure():
    app = _serve("404 Not Found", "text/html", b"gone")
    res = spec_scenarios.run_scenarios(_about_ir(), wsgi_app=app)
    assert res["ok"] is False
    f = res["failures"][0]
    assert "200" in f["expected"] and "404" in f["got"]
    assert f["node"] == "about_page"


# --------------------------------------------------------------------------
# S14.2 — an invalid IR is REFUSED, never "helpfully" executed (v149)
# --------------------------------------------------------------------------

def _v149_ir():
    """then.status 201 while the interface declares only 200 — the v149
    status guess, already an IR validation error at Phase A."""
    ir = _about_ir()
    sc = ir["nodes"]["about_page"]["scenarios"][0]
    sc["then"] = {"status": 201}
    return ir


def test_v149_status_guess_refuses_to_run():
    spy = _Spy(_serve("201 Created", "text/html", b"x"))
    res = spec_scenarios.run_scenarios(_v149_ir(), wsgi_app=spy)
    assert res["ok"] is False
    assert res["refused"], "an invalid IR must be refused with named errors"
    assert any("201" in e for e in res["refused"]), res["refused"]
    assert spy.calls == [], (
        "the runner issued requests against an INVALID IR — refusing beats "
        "running: %r" % spy.calls)
    assert res["passed"] == 0 and res["failures"] == []


def test_valid_ir_is_not_refused():
    res = spec_scenarios.run_scenarios(
        _about_ir(), wsgi_app=_serve("200 OK", "text/html", b"<p>ok</p>"))
    assert res["refused"] == []


# --------------------------------------------------------------------------
# S14.3 — absent when.body over required fields = named finding, no guess
# --------------------------------------------------------------------------

def _notes_ir(with_body):
    when = {"method": "POST", "path": "/notes"}
    if with_body:
        when["body"] = {"text": "hi"}
    return _ir({"notes": {
        "openapi": _openapi({"/notes": {
            "post": _op(201, "application/json", required=["text"])}}),
        "scenarios": [{
            "requirement": "notes", "when": when,
            "then": {"status": 201, "media": "application/json"}}]}})


def test_absent_body_with_required_fields_is_a_finding_never_a_guess():
    spy = _Spy(_serve("201 Created", "application/json", b'{"id": 1}'))
    res = spec_scenarios.run_scenarios(_notes_ir(with_body=False),
                                       wsgi_app=spy)
    assert res["incomplete"], "the gap must surface as a NAMED finding"
    gap = res["incomplete"][0]
    assert gap["requirement"] == "notes" and gap["node"] == "notes"
    assert "POST /notes" in gap["step"]
    assert "text" in gap["missing"], (
        "the finding must name the unvalued required fields: %r" % gap)
    assert ("POST", "/notes") not in spy.calls, (
        "the runner executed the scenario with an INVENTED body")
    assert res["failures"] == [], "a gap is a finding, not a fabricated red"
    assert res["passed"] == 0, "a skipped scenario must never count as green"


def test_present_body_is_executed_and_judged():
    spy = _Spy(_serve("201 Created", "application/json", b'{"id": 1}'))
    res = spec_scenarios.run_scenarios(_notes_ir(with_body=True),
                                       wsgi_app=spy)
    assert ("POST", "/notes") in spy.calls
    assert res["ok"] is True and res["passed"] == 1, res
    assert res["incomplete"] == []


# --------------------------------------------------------------------------
# S14.1 (state) — given.state replayed; a product that drops state reds
# --------------------------------------------------------------------------

def _roundtrip_ir():
    return _ir({"notes": {
        "openapi": _openapi({"/notes": {
            "post": _op(201, "application/json", required=["text"]),
            "get": _op(200, "application/json")}}),
        "scenarios": [{
            "requirement": "notes",
            "given": {"state": [{"method": "POST", "path": "/notes",
                                 "body": {"text": "first"}}]},
            "when": {"method": "GET", "path": "/notes"},
            "then": {"status": 200, "media": "application/json",
                     "body_check": {"json_subset":
                                    {"items": [{"text": "first"}]}}}}]}})


def _stateful_app():
    items = []

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD")
        if method == "POST":
            n = int(environ.get("CONTENT_LENGTH") or 0)
            doc = json.loads(environ["wsgi.input"].read(n) or b"{}")
            items.append({"id": len(items) + 1, "text": doc.get("text")})
            start_response("201 Created",
                           [("Content-Type", "application/json")])
            return [json.dumps({"id": len(items)}).encode()]
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"items": items}).encode()]
    return app


def test_state_replay_green_on_a_product_that_keeps_state():
    res = spec_scenarios.run_scenarios(_roundtrip_ir(),
                                       wsgi_app=_stateful_app())
    assert res["ok"] is True, res
    assert res["passed"] == 1


def test_state_replay_reds_a_product_that_drops_state():
    def amnesiac(environ, start_response):
        if environ.get("REQUEST_METHOD") == "POST":
            start_response("201 Created",
                           [("Content-Type", "application/json")])
            return [b'{"id": 1}']
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"items": []}']
    res = spec_scenarios.run_scenarios(_roundtrip_ir(), wsgi_app=amnesiac)
    assert res["ok"] is False
    f = res["failures"][0]
    assert f["node"] == "notes" and "GET /notes" in f["step"]
    assert "first" in f["expected"], f


# --------------------------------------------------------------------------
# S14.4 — engine-owned deterministic env value factory
# --------------------------------------------------------------------------

_PATH_RULE = "filesystem path of the file where records persist"


def test_env_factory_path_rule_gets_a_fresh_workdir_path(tmp_path):
    f = spec_scenarios.EnvValueFactory(str(tmp_path))
    a = f.value("RECORDS_FILE", _PATH_RULE)
    assert a == f.value("RECORDS_FILE", _PATH_RULE), (
        "same var name -> same value within one run")
    assert str(pathlib.Path(a).resolve()).startswith(
        str(tmp_path.resolve())), (
        "a path-rule var must live UNDER the run workspace: %r" % a)
    b = f.value("OTHER_STORE", "path to the database file")
    assert b != a, "distinct vars must never share a value"


def test_env_factory_is_fresh_per_run(tmp_path):
    a = spec_scenarios.EnvValueFactory(str(tmp_path)).value(
        "RECORDS_FILE", _PATH_RULE)
    b = spec_scenarios.EnvValueFactory(str(tmp_path)).value(
        "RECORDS_FILE", _PATH_RULE)
    assert a != b, ("a NEW run must get a FRESH temp path — a shared value "
                    "is cross-run memory, the rejected cache crutch")


def test_env_factory_derives_from_the_rule_not_the_name(tmp_path):
    # a name carrying NO filesystem token still gets a path when its RULE
    # names one — behaviour comes from the IR env entry, never a whitelist
    # of product-specific literals
    f = spec_scenarios.EnvValueFactory(str(tmp_path))
    v = f.value("ZZZ_QQQ", "the file path used for persistence")
    assert str(pathlib.Path(v).resolve()).startswith(str(tmp_path.resolve()))


def test_env_factory_non_path_rule_is_a_deterministic_token(tmp_path):
    f = spec_scenarios.EnvValueFactory(str(tmp_path))
    v = f.value("SERVICE_MODE", "operating mode label")
    assert isinstance(v, str) and v
    assert v == f.value("SERVICE_MODE", "operating mode label")
    assert not str(v).startswith(str(tmp_path)), (
        "a non-filesystem rule must not be answered with a path")


def _greet_ir():
    return _ir({"greet": {
        "env": [{"name": "GREETING_TEXT", "rule": "the greeting text"}],
        "openapi": _openapi({"/greet": {
            "get": _op(200, "application/json")}}),
        "scenarios": [{
            "requirement": "greet",
            "given": {"env": {"GREETING_TEXT": "hello-from-ir"}},
            "when": {"method": "GET", "path": "/greet"},
            "then": {"status": 200, "media": "application/json",
                     "body_check": {"contains": "hello-from-ir"}}}]}})


def test_given_env_reaches_the_live_app():
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps(
            {"text": os.environ.get("GREETING_TEXT", "")}).encode()]
    res = spec_scenarios.run_scenarios(_greet_ir(), wsgi_app=app)
    assert res["ok"] is True, res


def test_app_ignoring_given_env_reds():
    app = _serve("200 OK", "application/json", b'{"text": "static"}')
    res = spec_scenarios.run_scenarios(_greet_ir(), wsgi_app=app)
    assert res["ok"] is False
    assert "hello-from-ir" in res["failures"][0]["expected"]
