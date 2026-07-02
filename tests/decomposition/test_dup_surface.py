"""Decomposition-quality: a LATE requirement's spec must scope to its DELTA, not
re-state a surface an existing module already owns. _dup_surface_findings is the
deterministic, model-independent detector (a registry of increasing-precision
methods) the engine runs before the LLM spec reviewer — the code half of the
fix for the v040 'web_ui re-specs the whole notes service' duplicate.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import run_engine as eng    # noqa: E402  (re-exports the runner)

dup = eng._dup_surface_findings


def _mod(rel, body):
    return (rel, rel.split("/")[-1][:-3], body)


# Realistic OWNER bodies: a module owns a route only with real CODE DISPATCH
# (path == '/x'), never a bare route mention — see _served_routes. Bare
# route-line fixtures silently stopped exercising the flag path after the v101
# ownership hardening (a prose mention no longer credits ownership).
_NOTES_SERVED = (
    "def wsgi_app(environ, start_response):\n"
    "    path = environ['PATH_INFO']; method = environ['REQUEST_METHOD']\n"
    "    if path == '/notes' and method == 'POST': return _create()\n"
    "    if path == '/notes' and method == 'GET': return _list()\n")
_HEALTH_SERVED = (
    "def health(environ, start_response):\n"
    "    path = environ['PATH_INFO']; method = environ['REQUEST_METHOD']\n"
    "    if path == '/health' and method == 'GET': return _ok()\n")


def test_route_redeclare_when_no_new_route():
    mods = [_mod("src/notes_api.py", _NOTES_SERVED)]
    spec = "This service exposes POST /notes and GET /notes round-tripping a note."
    f = dup(spec, mods)
    assert any(x.startswith("route-redeclare") for x in f), f
    assert "notes_api" in " ".join(f)


def test_clean_when_spec_adds_a_new_route():
    mods = [_mod("src/notes_api.py", _NOTES_SERVED)]
    spec = "Add a new GET /ui HTML page that lists notes and offers an add form."
    assert dup(spec, mods) == []


def test_clean_when_late_request_adds_a_delete_route():
    # v143: the human late-injects "delete a note". DELETE /notes/{id} is a NEW
    # verb on the notes surface — it MUST NOT read as a duplicate of the existing
    # POST/GET /notes, so scope-lint stays silent and the leaf is built (not
    # folded/reworked as an empty delta).
    mods = [_mod("src/notes_api.py", _NOTES_SERVED)]
    spec = "Add DELETE /notes/{id} that removes the note with that id."
    assert dup(spec, mods) == []


def test_surface_subset_flags_whole_surface_owned():
    mods = [_mod("src/notes_api.py", _NOTES_SERVED + _HEALTH_SERVED)]
    spec = "Serve GET /notes and GET /health exactly as the service requires."
    f = dup(spec, mods)
    assert any(x.startswith("surface-subset") for x in f), f


def test_scope_breadth_flags_respec_of_whole_service():
    # the v040 web_ui case: spec re-states routes across TWO existing modules
    # while adding only one sliver of its own (GET /ui)
    mods = [_mod("src/notes_api.py", _NOTES_SERVED),
            _mod("src/health.py", _HEALTH_SERVED)]
    spec = ("A WSGI app exposing POST /notes, GET /notes, GET /health and a new "
            "GET /ui page.")
    f = dup(spec, mods)
    assert any(x.startswith("scope-breadth") for x in f), f


def test_silent_when_no_structural_surface():
    # a pure presentation refinement ('make it nice') declares no route/symbol
    mods = [_mod("src/notes_api.py", "GET /notes\n")]
    spec = "Make the listing look clean and friendly, a tidy heading and layout."
    assert dup(spec, mods) == []


def test_silent_when_no_existing_modules():
    assert dup("POST /notes and GET /notes", []) == []
