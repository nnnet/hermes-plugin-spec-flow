"""Intention board (B) + content-hash (C): a duplicate intent is a
cache-hit, not a second implementation. Variant A keeps module names
unique; B+C catch the SAME functionality proposed under different names."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import claims  # noqa: E402


# ─── C: content-hash of intent ────────────────────────────────────────

def test_same_intent_same_hash_despite_punctuation_and_case():
    a = claims.intent_hash("Cart API", "Add an item to the cart.")
    b = claims.intent_hash("cart  api", "ADD an item, to the cart!!!")
    assert a == b


def test_different_intent_different_hash():
    a = claims.intent_hash("Cart API", "Add an item to the cart")
    b = claims.intent_hash("Payout API", "Transfer money to a seller")
    assert a != b


# ─── B: claim board lifecycle ─────────────────────────────────────────

def test_fresh_intent_is_granted():
    b = claims.ClaimBoard()
    v = b.claim("cart", "cart", "Cart API", "add item")
    assert v["verdict"] == "granted"


def test_same_node_revisit_is_idempotent():
    b = claims.ClaimBoard()
    v1 = b.claim("cart", "cart", "Cart API", "add item")
    v2 = b.claim("cart", "cart", "Cart API", "add item")
    assert v1["verdict"] == v2["verdict"] == "granted"


def test_duplicate_intent_after_completion_is_cache_hit():
    b = claims.ClaimBoard()
    v1 = b.claim("cart_a", "cart_a", "Cart API", "add item to cart")
    b.complete("cart_a", v1["hash"])
    # a DIFFERENT node, SAME intent → duplicate pointing at the owner
    v2 = b.claim("cart_b", "cart_b", "Cart API", "ADD item to cart!")
    assert v2["verdict"] == "duplicate"
    assert v2["owner"] == "cart_a" and v2["owner_module"] == "cart_a"


def test_in_progress_duplicate_proceeds_deadlock_free():
    b = claims.ClaimBoard()
    b.claim("cart_a", "cart_a", "Cart API", "add item")   # not completed
    v2 = b.claim("cart_b", "cart_b", "Cart API", "add item")
    # owner still in-progress → grant (proceed) rather than block
    assert v2["verdict"] == "granted"


# ─── re-export cache-hit through the implementer ──────────────────────

class _WS:
    def __init__(self, root):
        self.root = str(root)
        self.written = {}

    def _write(self, rel, body, kind):
        p = pathlib.Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        self.written[rel] = body


def test_write_reexport_creates_delegating_module(tmp_path):
    from harness import role_worker as rw
    ws = _WS(tmp_path)
    rw._write_reexport(ws, "cart_b", "cart_a", "cart_a")
    assert "from cart_a import *" in ws.written["src/cart_b.py"]
    assert "import cart_b" in ws.written["tests/test_cart_b.py"]


def test_implement_dedups_without_calling_the_model(tmp_path, monkeypatch):
    # the SECOND leaf with the same intent must NOT reach the model — it
    # re-exports the first. We pre-complete the owner's claim, then drive
    # implement() for a different node with the same intent.
    from harness import role_worker as rw
    from harness import llm_backend as lb

    # implement() derives the intent from the spec FILE — pin it so the
    # seeded owner and the duplicate compute the same hash
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "add an item")
    claims.configure(True)
    owner = claims.BOARD.claim("cart_a", "cart_a", "Cart API", "add an item")
    claims.BOARD.complete("cart_a", owner["hash"])

    # if the duplicate path is NOT taken, the real chat implementer runs
    # pytest in a subprocess and raises — reaching the re-export proves
    # the model was skipped
    impl = rw.make_implementer()
    ws = _WS(tmp_path / "wk")
    impl({"node": "cart_b", "title": "Cart API", "module": "cart_b",
          "spec": "specs/cart.md", "workspace": ws, "depth": 1})
    assert "from cart_a import *" in ws.written["src/cart_b.py"]
    claims.configure(False)
    lb.configure_workers(None)
