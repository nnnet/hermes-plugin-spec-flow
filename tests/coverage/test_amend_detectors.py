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
