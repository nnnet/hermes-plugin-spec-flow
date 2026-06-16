"""Provider adapters — the WHERE/HOW a RoleTask runs (doc §4).

Every adapter implements the ONE uniform contract from the architecture doc §3:

    execute(RoleTask) -> RoleResult

so the engine never branches on provider. `local` is NOT here — it is the
built-in LLM call in ``role_worker`` and stays byte-for-byte unchanged; this
package only holds the REMOTE adapters (`hermes`, `mission-control`, `a2a`).

Transport is INJECTABLE on every adapter (a ``transport=`` callable defaulting
to a urllib-based one), so tests drive a fake transport and NO live network is
opened in the suite.
"""
from __future__ import annotations

# Import the adapter modules for their @register side effects, then expose the
# registry lookups. Order is irrelevant — each registers under its own name.
from .base import (Provider, Transport, get_provider, register,
                   urllib_transport, RoleTaskLike, RoleResultLike)
from . import hermes as _hermes          # noqa: F401  (registers 'hermes')
from . import mission_control as _mc     # noqa: F401  (registers 'mission-control')
from . import a2a as _a2a                # noqa: F401  (registers 'a2a')

__all__ = ["Provider", "Transport", "get_provider", "register",
           "urllib_transport", "RoleTaskLike", "RoleResultLike"]
