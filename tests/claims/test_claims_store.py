"""П15: pluggable ClaimStore. The board logic is unchanged; only WHERE
claims live varies. The sqlite backend is durable — a stopped run that
reopens the same db RESTORES its board, so an intent finished before the
stop is still a cache-hit after --resume."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import claims  # noqa: E402


# ─── factory ──────────────────────────────────────────────────────────

def test_make_store_defaults_to_memory():
    s = claims.make_store()
    assert isinstance(s, claims.ClaimStore)
    assert not isinstance(s, claims.SqliteClaimStore)


def test_make_store_sqlite_uses_run_dir(tmp_path):
    s = claims.make_store({"backend": "sqlite"}, run_dir=str(tmp_path))
    assert isinstance(s, claims.SqliteClaimStore)
    assert s.path == tmp_path / "claims.db"
    s.close()


def test_make_store_sqlite_explicit_path(tmp_path):
    p = tmp_path / "nested" / "x.db"
    s = claims.make_store({"backend": "sqlite", "path": str(p)})
    assert s.path == p and p.is_file()
    s.close()


def test_make_store_unknown_backend_raises():
    try:
        claims.make_store({"backend": "redis"})
    except ValueError as e:
        assert "redis" in str(e)
    else:
        assert False, "unknown backend must raise"


# ─── sqlite-backed board behaves like the in-memory one ───────────────

def test_sqlite_board_dedups_like_memory(tmp_path):
    store = claims.make_store({"backend": "sqlite"}, run_dir=str(tmp_path))
    b = claims.ClaimBoard(store=store)
    v1 = b.claim("cart_a", "cart_a", "Cart API", "add item to cart")
    b.complete("cart_a", v1["hash"])
    v2 = b.claim("cart_b", "cart_b", "Cart API", "ADD item to cart!")
    assert v2["verdict"] == "duplicate" and v2["owner"] == "cart_a"
    b.close()


# ─── persistence + restore (the stop/restart contract) ────────────────

def test_sqlite_restores_board_after_restart(tmp_path):
    db = str(tmp_path / "claims.db")
    # run 1: finish an intent, then "stop" (close the board)
    b1 = claims.ClaimBoard(store=claims.SqliteClaimStore(db))
    v = b1.claim("pay_a", "pay_a", "Payout API", "transfer money to a seller")
    b1.complete("pay_a", v["hash"])
    b1.close()

    # run 2 (--resume): a FRESH board over the SAME db restores the claim —
    # a different node with the same intent is still a cache-hit
    b2 = claims.ClaimBoard(store=claims.SqliteClaimStore(db))
    snap = b2.snapshot()
    assert v["hash"] in snap and snap[v["hash"]]["state"] == "done"
    v2 = b2.claim("pay_b", "pay_b", "Payout API", "Transfer money to a seller.")
    assert v2["verdict"] == "duplicate" and v2["owner"] == "pay_a"
    b2.close()


def test_memory_store_does_not_persist(tmp_path):
    # the default store is intentionally non-durable — a new board forgets
    b1 = claims.ClaimBoard()
    v = b1.claim("x_a", "x_a", "X API", "do x")
    b1.complete("x_a", v["hash"])
    b1.close()
    b2 = claims.ClaimBoard()
    assert b2.snapshot() == {}


def test_configure_injects_a_store(tmp_path):
    store = claims.SqliteClaimStore(str(tmp_path / "c.db"))
    claims.configure(True, store=store)
    assert claims.BOARD is not None
    v = claims.BOARD.claim("n", "n", "T", "r")
    assert v["verdict"] == "granted"
    claims.configure(False)
    assert claims.BOARD is None
