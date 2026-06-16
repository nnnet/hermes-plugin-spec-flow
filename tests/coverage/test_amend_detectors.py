"""B3 surface-overlap detector REGISTRY — universal, extensible, manageable.

A requirement that refines an existing surface must be matched to its owner even
when it names NO route and NO file ("polish the front-end", "present the catalog
nicely"). Several orthogonal detectors cover that; none is domain-tailored, so
the same machinery routes a notes page, a CSV export, or a payment receipt — it
gives no special help to the web/ui case.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

find = eng._amend_find_owner


def _mods(*pairs):
    # (stem, body) -> (rel, stem, body)
    return [(f"src/{s}.py", s, b) for s, b in pairs]


# -- the hard case the user named: NO route, NO filename, worded as "front-end"
def test_frontend_wording_without_route_or_filename(tmp_path):
    web = ("web_ui", 'def render_notes_page(notes):\n'
                     '    return "<html>...notes list...</html>"\n')
    db = ("db", "def connect():\n    pass\n"
                "def insert_note(text):\n    pass\n")
    owner = find("The front-end that shows the notes list should look polished "
                 "and friendly.", _mods(web, db))
    assert owner == "src/web_ui.py"      # symbol/token overlap, not a route


# -- universal: same machinery on a NON-web surface (CSV export), no UI words
def test_universal_non_web_surface_export(tmp_path):
    exp = ("export_csv", "def export_rows_to_csv(rows):\n    return ''\n")
    api = ("notes_api", "def post_note(payload, query):\n    return 200, {}\n")
    owner = find("Improve the CSV export so the rows are sorted by date.",
                 _mods(exp, api))
    assert owner == "src/export_csv.py"


# -- explicit route still works (cheapest signal)
def test_route_match(tmp_path):
    web = ("web_ui", '# GET /ui\ndef page():\n    return "<html></html>"\n')
    owner = find("The existing GET /ui page must paginate.", _mods(web))
    assert owner == "src/web_ui.py"


# -- explicit file mention
def test_file_mention(tmp_path):
    web = ("web_ui", "def page():\n    return ''\n")
    owner = find("Refactor src/web_ui.py to add a header.", _mods(web))
    assert owner == "src/web_ui.py"


# -- genuinely new surface → no owner (no false amend)
def test_no_overlap_returns_none(tmp_path):
    db = ("db", "def connect():\n    pass\n")
    api = ("notes_api", "def post_note(p, q):\n    return 200, {}\n")
    owner = find("Add rate limiting middleware for abusive clients.",
                 _mods(db, api))
    assert owner is None


# -- the detector set is MANAGEABLE: restrict to routes only ⇒ a token-only
#    overlap no longer matches (proves selection works, not just defaults)
def test_method_selection_restricts_signals(tmp_path):
    web = ("web_ui", 'def render_notes_page(notes):\n    return "<html></html>"\n')
    # token/symbol overlap would match, but we allow only the route detector
    owner = find("Make the notes presentation nicer.", _mods(web),
                 methods=["shared_route"])
    assert owner is None
    # with the symbol detector enabled it matches again
    owner2 = find("Make the notes presentation nicer.", _mods(web),
                  methods=["symbol_overlap"])
    assert owner2 == "src/web_ui.py"


# -- the registry is EXTENSIBLE: a newly registered detector participates
def test_registry_is_extensible(tmp_path):
    @eng._amend_detector("always_one")
    def _always(feat, mod):          # noqa: ANN001
        return 1.0
    try:
        owner = find("anything at all", _mods(("whatever", "x = 1\n")),
                     methods=["always_one"])
        assert owner == "src/whatever.py"
    finally:
        eng._AMEND_DETECTORS.pop("always_one", None)


# -- PRECISION: a new feature that shares only a couple of incidental words
#    with an unrelated module must NOT be mis-routed into it (false amend
#    corrupts that module — worse than forking). token_overlap is held to a
#    higher floor; only a real symbol/route/file match may route.
def test_low_overlap_new_feature_not_misrouted(tmp_path):
    listing = ("catalog", "def render_catalog(items):\n    return ''\n"
                          "# product items grid layout price\n")
    owner = find("Add structured audit logging for every write operation.",
                 _mods(listing), methods=["token_overlap"])
    assert owner is None


# -- best owner wins when several modules share tokens (highest overlap)
def test_best_owner_wins(tmp_path):
    web = ("web_ui", "def render_notes_page(notes):\n    return ''\n"
                     "# notes list table form html\n")
    db = ("db", "def insert_note(text):\n    pass\n")   # only 'note(s)' overlap
    owner = find("Present the notes list in a nicer table layout.",
                 _mods(web, db))
    assert owner == "src/web_ui.py"


# -- #1: candidate matchable from its SPEC text alone (no code file yet). The
#    v0NN miss: a late 'make it nice' forked because src/web_ui.py did not exist
#    at match time even though the web_ui SPEC did. The body fed to the matcher
#    is spec markdown here (no defs) — a defined-symbol match is impossible, so
#    the looser surface signal must carry it.
def test_spec_only_candidate_routes_without_a_file(tmp_path):
    spec_body = ("# Web page\n- Node: web_ui\n"
                 "Renders the notes list as an HTML page with an add form.\n")
    owner = find("Make the notes list and the add form look tidy and friendly.",
                 _mods(("web_ui", spec_body)))
    assert owner == "src/web_ui.py"


# -- #3 surface_overlap: shares >=2 surfaces (notes, form, list) but no single
#    detector crossed its bar; the structural backstop routes it.
def test_surface_overlap_routes_on_multiple_weak_surfaces(tmp_path):
    web = ("web_ui", "An HTML page that lists notes and shows an add form.\n")
    owner = find("Tidy the notes, the list and the form.", _mods(web),
                 methods=["surface_overlap"])
    assert owner == "src/web_ui.py"


def test_surface_overlap_silent_on_single_incidental_word(tmp_path):
    other = ("billing", "Charges a customer card and emits a receipt.\n")
    owner = find("Tidy the notes list and the add form.", _mods(other),
                 methods=["surface_overlap"])
    assert owner is None


# -- fastest-first SHORT-CIRCUIT: once an early, cheap detector finds an owner
#    the later/expensive ones (here a sentinel that would steal the match) never
#    run. Proven by registering a greedy detector AFTER the matching one and
#    asserting the cheap match wins.
def test_short_circuit_skips_later_detectors(tmp_path):
    calls = {"n": 0}

    @eng._amend_detector("greedy_sentinel")
    def _greedy(feat, mod):          # noqa: ANN001
        calls["n"] += 1
        return 1.0
    try:
        web = ("web_ui", "def page():\n    return ''\n# GET /ui\n")
        owner = find("The GET /ui page must paginate.", _mods(web),
                     methods=["shared_route", "greedy_sentinel"])
        assert owner == "src/web_ui.py"
        assert calls["n"] == 0       # shared_route matched first → sentinel skipped
    finally:
        eng._AMEND_DETECTORS.pop("greedy_sentinel", None)


# -- #2 llm_router: deterministic layers all empty → the injected router fires
#    and its answer is honoured (id) / forks (new / error).
def test_llm_router_fires_only_when_deterministic_empty(tmp_path):
    db = ("db", "def connect():\n    return 1\n")
    # statement shares nothing structural with db → all detectors 0
    stmt = "Add an unrelated background reaper for stale sessions."

    def router_picks(statement, modules, cand_text):
        return "src/db.py"
    owner = find(stmt, _mods(db), llm=router_picks)
    assert owner == "src/db.py"

    def router_new(statement, modules, cand_text):
        return None              # 'new'
    assert find(stmt, _mods(db), llm=router_new) is None

    def router_boom(statement, modules, cand_text):
        raise RuntimeError("backend down")
    assert find(stmt, _mods(db), llm=router_boom) is None  # safe fork


def test_llm_router_not_called_when_a_detector_matches(tmp_path):
    web = ("web_ui", "def page():\n    return ''\n# GET /ui\n")
    fired = {"n": 0}

    def router(statement, modules, cand_text):
        fired["n"] += 1
        return "src/web_ui.py"
    owner = find("The GET /ui page must paginate.", _mods(web), llm=router)
    assert owner == "src/web_ui.py"
    assert fired["n"] == 0        # a deterministic detector already decided


def test_shared_domain_noun_abstains_then_llm_routes(tmp_path):
    # The красивый_вид class: a presentation requirement whose only lexical
    # overlap is the project's shared domain nouns (notes / list), which BOTH a
    # storage and a web module own. Deterministic layers must ABSTAIN — matching
    # storage on a coincidental list_notes name would be a false amend; the
    # semantic LLM router then routes presentation into the web module.
    stmt = ("Make the notes nice to read — a tidy list, a clear heading, an "
            "easy way to add one; improve the existing presentation.")
    web = ("web_ui", "def render_notes_page(notes):\n    return '<html>'\n"
                     "# GET /ui add form list heading layout\n")
    db = ("db", "def insert_note(text):\n    return 1\n"
                "def list_notes():\n    return []\n# /notes sqlite storage\n")
    mods = _mods(web, db)
    assert find(stmt, mods) is None          # abstains, no false db match

    def router(statement, modules, cand_text):
        return "src/web_ui.py"
    assert find(stmt, mods, llm=router) == "src/web_ui.py"


def test_distinctive_multitoken_still_routes(tmp_path):
    # distinctiveness must NOT muzzle a real match: a token unique to ONE
    # candidate still routes deterministically (no LLM needed).
    web = ("web_ui", "def paginate_table(rows):\n    return rows\n"
                     "# pagination table column sorting\n")
    db = ("db", "def insert_note(text):\n    return 1\n")
    owner = find("Add column sorting to the paginated table.", _mods(web, db))
    assert owner == "src/web_ui.py"
