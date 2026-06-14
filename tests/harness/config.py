"""Single config-resolution point for the spec-flow harness.

Resolution order is: a case `workers:` block (handled at the call site via
``WORKERS_CFG``) > an environment variable > error. There are NO literal
defaults in the Python anymore — the default floor lives in ``tests/.test.env``
(which a real exported env var still overrides, and a case YAML overrides on
top of that). This kills hidden hardcoded defaults that masked what a run
actually used.
"""
from __future__ import annotations

import os
from pathlib import Path

_PREFIX = "SPEC_FLOW_"
_RAISE = object()
_loaded = False


class SpecFlowConfigError(RuntimeError):
    """A required config key is set nowhere — no case value, no env var, and
    (by design) no hardcoded default. The fix is to add it to the case
    ``workers:`` block or to ``tests/.test.env``."""


def _env_path() -> Path:
    # harness/ -> tests/.test.env
    return Path(__file__).resolve().parent.parent / ".test.env"


def load_test_env(path: "Path | None" = None) -> None:
    """Seed ``os.environ`` from ``tests/.test.env`` with setdefault semantics —
    a value already exported (real env or the launcher) always wins over the
    file. Idempotent for the default path."""
    global _loaded
    if _loaded and path is None:
        return
    p = path or _env_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        if path is None:
            _loaded = True
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)
    if path is None:
        _loaded = True


def env(name: str, cast=str, default=_RAISE):
    """Resolve ``SPEC_FLOW_<name>`` from the environment (seeded by
    ``.test.env``). Raises :class:`SpecFlowConfigError` when the key is unset
    and no explicit ``default`` is passed — there is no hidden literal
    fallback. ``cast`` converts the string (e.g. ``int``, ``float``)."""
    load_test_env()
    key = name if name.startswith(_PREFIX) else _PREFIX + name
    raw = os.environ.get(key)
    if raw is None or raw == "":
        if default is _RAISE:
            raise SpecFlowConfigError(
                f"{key} is set nowhere (no case value, no env, no .test.env). "
                f"Add it to the case workers block or tests/.test.env.")
        return default
    if cast is str:
        return raw
    if cast is bool:
        return raw not in ("", "0", "false", "False", "no")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        if default is not _RAISE:
            return default
        raise
