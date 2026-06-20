"""On resume, a cached leaf is re-validated against the CURRENT deterministic
gate: a leaf that passed when journaled but fails a gate tightened SINCE (a new
plugin check, not a spec edit) is a cache MISS and re-runs — so the fast
'fix the plugin, resume the checkpoint' loop applies new gates without a full
rebuild."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import spec_flow_runner as sfr   # noqa: E402
from harness import contract_checks as cc   # noqa: E402


class _WS:
    enabled = True

    def __init__(self, root):
        self.root = str(root)


class _Eng:
    # the realness checker is INJECTED (the plugin never imports the harness)
    def __init__(self, root, check=cc.realness_violations):
        self.workspace = _WS(root)
        self._realness_check = check


def _src(tmp, name, body):
    d = tmp / "src"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_cached_artifact_failing_a_current_gate_is_stale(tmp_path):
    # the cached code hangs every POST (unbounded wsgi read) -> the now-tightened
    # gate flags it -> stale (cache miss) -> the leaf re-runs
    _src(tmp_path, "api.py",
         "def wsgi_app(environ, start_response):\n"
         "    body = environ['wsgi.input'].read()\n"
         "    return [body]\n")
    stale = sfr.Engine._cached_artifact_stale(_Eng(tmp_path), "api")
    assert stale and "UNBOUNDED" in stale


def test_cached_artifact_still_clean_is_reused(tmp_path):
    # a correct bounded read passes the gate -> '' -> the cache hit stands
    _src(tmp_path, "api.py",
         "def wsgi_app(environ, start_response):\n"
         "    n = int(environ.get('CONTENT_LENGTH') or 0)\n"
         "    body = environ['wsgi.input'].read(n)\n"
         "    return [body]\n")
    assert sfr.Engine._cached_artifact_stale(_Eng(tmp_path), "api") == ""


def test_no_workspace_is_never_stale():
    class _Off:
        workspace = type("W", (), {"enabled": False, "root": None})()
        _realness_check = cc.realness_violations
    assert sfr.Engine._cached_artifact_stale(_Off(), "api") == ""
