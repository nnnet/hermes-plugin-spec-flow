"""Config resolver: the single place that turns SPEC_FLOW_* keys into values.
There are no literal defaults in the harness anymore — the floor is
tests/.test.env, a real env var overrides the file (setdefault), and a missing
required key raises instead of silently picking a hidden default."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import config as cfg  # noqa: E402


def test_env_reads_value(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_LLM_RETRIES", "7")
    assert cfg.env("LLM_RETRIES", int) == 7
    # accepts the full key too
    assert cfg.env("SPEC_FLOW_LLM_RETRIES", int) == 7


def test_env_missing_raises(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_DOES_NOT_EXIST", raising=False)
    try:
        cfg.env("DOES_NOT_EXIST")
        assert False, "must raise on a key set nowhere"
    except cfg.SpecFlowConfigError:
        pass


def test_env_explicit_default_when_missing(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_DOES_NOT_EXIST", raising=False)
    assert cfg.env("DOES_NOT_EXIST", default="x") == "x"


def test_env_empty_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_EMPTY", "")
    assert cfg.env("EMPTY", default="fallback") == "fallback"


def test_env_bool_cast(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_FLAG", "0")
    assert cfg.env("FLAG", bool) is False
    monkeypatch.setenv("SPEC_FLOW_FLAG", "1")
    assert cfg.env("FLAG", bool) is True


def test_load_test_env_setdefault_keeps_real_env(monkeypatch, tmp_path):
    # a value already in the environment is NOT overwritten by the file
    monkeypatch.setenv("SPEC_FLOW_LLM_MODEL", "real/model")
    f = tmp_path / ".test.env"
    f.write_text("SPEC_FLOW_LLM_MODEL=file/model\nSPEC_FLOW_NEWKEY=fromfile\n")
    cfg.load_test_env(f)
    import os
    assert os.environ["SPEC_FLOW_LLM_MODEL"] == "real/model"   # env wins
    assert os.environ["SPEC_FLOW_NEWKEY"] == "fromfile"        # file fills gap


def test_load_test_env_parses_export_and_quotes(tmp_path):
    import os
    f = tmp_path / ".test.env"
    f.write_text('export SPEC_FLOW_Q="quoted value"\n# comment\n\nSPEC_FLOW_P=plain\n')
    os.environ.pop("SPEC_FLOW_Q", None)
    os.environ.pop("SPEC_FLOW_P", None)
    cfg.load_test_env(f)
    assert os.environ["SPEC_FLOW_Q"] == "quoted value"
    assert os.environ["SPEC_FLOW_P"] == "plain"
