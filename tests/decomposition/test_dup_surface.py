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


def test_route_redeclare_when_no_new_route():
    mods = [_mod("src/notes_api.py", "POST /notes\nGET /notes\n")]
    spec = "This service exposes POST /notes and GET /notes round-tripping a note."
    f = dup(spec, mods)
    assert any(x.startswith("route-redeclare") for x in f), f
    assert "notes_api" in " ".join(f)


def test_clean_when_spec_adds_a_new_route():
    mods = [_mod("src/notes_api.py", "POST /notes\nGET /notes\n")]
    spec = "Add a new GET /ui HTML page that lists notes and offers an add form."
    assert dup(spec, mods) == []


def test_surface_subset_flags_whole_surface_owned():
    mods = [_mod("src/notes_api.py", "GET /notes\nGET /health\n")]
    spec = "Serve GET /notes and GET /health exactly as the service requires."
    f = dup(spec, mods)
    assert any(x.startswith("surface-subset") for x in f), f


def test_scope_breadth_flags_respec_of_whole_service():
    # the v040 web_ui case: spec re-states routes across TWO existing modules
    # while adding only one sliver of its own (GET /ui)
    mods = [_mod("src/notes_api.py", "POST /notes\nGET /notes\n"),
            _mod("src/health.py", "GET /health\n")]
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
