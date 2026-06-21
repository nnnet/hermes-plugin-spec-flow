"""462: memory_loss data source — a recorded depends_on decision the failing
node didn't honour is read from the run's intentions board (_node_registry +
_module_names) and fed to the doctor as evidence['missing_decisions']; the
memory_absent detector then fires. Decided by the artifact, not the model."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine, Workspace   # noqa: E402
import spec_flow_diagnosers as diag              # noqa: E402


def _engine(tmp):
    e = Engine.__new__(Engine)
    e.workspace = Workspace(root=tmp, enabled=True).open()
    e._node_registry = {"db_core": "Database core", "api": "API layer"}
    e._module_names = {"db_core": "db_core", "api": "api"}
    return e


def _write(tmp, rel, body):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(body)


def test_no_depends_on_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        assert e._missing_decisions({"id": "api"}, "api") == []


def test_forgotten_dependency_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        # api depends on db_core but its module never references db_core
        _write(tmp, "src/api.py", "def handler(req):\n    return 200, {}\n")
        miss = e._missing_decisions({"id": "api", "depends_on": ["db_core"]}, "api")
        assert miss and "db_core" in miss[0]


def test_honoured_dependency_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        # the module imports db_core -> decision honoured -> nothing missing
        _write(tmp, "src/api.py", "import db_core\n\ndef h(r):\n    return db_core.q()\n")
        assert e._missing_decisions({"id": "api", "depends_on": ["db_core"]}, "api") == []


def test_unknown_dep_not_counted():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/api.py", "x = 1\n")
        # 'ghost' is not in the registry -> not a recorded decision -> ignored
        assert e._missing_decisions({"id": "api", "depends_on": ["ghost"]}, "api") == []


def test_detector_fires_on_missing_decisions():
    d = diag.Diagnosers()
    findings = d.run(node="api", gate="spec_review", verdict="REJECT",
                     evidence={"missing_decisions": ["reuse db_core (...)"]},
                     context=None)
    # the memory_loss concept is carried by the context_loss cause id
    assert "context_loss" in [f.cause for f in findings]
