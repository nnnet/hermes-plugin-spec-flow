"""#10 / П13: worker specialization. A node's specialty (explicit in the
case, project default, or auto-inferred) routes the implementer to a
specialty-specific model chain. chain_for honors it; unknown specialties
fall back to the role chain."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import specialty as sp  # noqa: E402
from harness import llm_backend as lb  # noqa: E402


# ─── auto-inference ───────────────────────────────────────────────────

def test_infer_frontend_from_title():
    assert sp.infer_specialty("Catalog HTML page with search form") == "frontend"


def test_infer_database_from_spec():
    assert sp.infer_specialty("Seller store",
                              "persist sellers in a sql table with an index") \
        == "database"


def test_infer_payments():
    assert sp.infer_specialty("Seller payout disbursement") == "payments"


def test_infer_none_when_generic():
    assert sp.infer_specialty("Misc helper", "do a thing") == ""


# ─── resolve precedence: explicit → project default → auto ────────────

def test_explicit_node_specialty_wins():
    node = {"title": "HTML page", "specialty": "api"}
    # explicit 'api' beats the inferred 'frontend'
    assert sp.resolve_specialty(node, {}, set(), auto=True) == "api"


def test_project_default_when_no_explicit():
    node = {"title": "generic"}
    out = sp.resolve_specialty(node, {"default_specialty": "api"},
                               set(), auto=True)
    assert out == "api"


def test_auto_used_when_no_explicit_or_default():
    node = {"title": "Login token session"}
    assert sp.resolve_specialty(node, {}, set(), auto=True) == "auth"


def test_auto_off_yields_empty():
    node = {"title": "Login token session"}
    assert sp.resolve_specialty(node, {}, set(), auto=False) == ""


def test_available_set_restricts():
    node = {"title": "HTML page"}     # infers frontend
    # frontend not in the available set → rejected, falls through to ''
    assert sp.resolve_specialty(node, {}, {"api", "database"}, auto=True) == ""
    assert sp.resolve_specialty(node, {}, {"frontend"}, auto=True) == "frontend"


# ─── chain_for routing ────────────────────────────────────────────────

def test_chain_for_specialty_overrides_role_chain():
    lb.configure_workers({
        "implementer": {"models": ["role/default"],
                        "specialties": {"frontend": {"models": ["fe/model"]}}}})
    assert lb.chain_for("implementer")[0] == "role/default"
    assert lb.chain_for("implementer", "frontend")[0] == "fe/model"
    # unknown specialty → role chain
    assert lb.chain_for("implementer", "ghost")[0] == "role/default"
    lb.configure_workers(None)


def test_model_for_specialty():
    lb.configure_workers({
        "implementer": {"model": "role/default",
                        "specialties": {"db": {"model": "db/model"}}}})
    assert lb.model_for("implementer", "db") == "db/model"
    assert lb.model_for("implementer") == "role/default"
    lb.configure_workers(None)
