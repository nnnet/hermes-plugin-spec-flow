"""Adapter ABC, registry and the default urllib transport (doc §3/§4).

Why: a single Provider interface + a name→adapter registry lets the call path
dispatch ``provider != local`` to a remote adapter without the engine knowing
which one; the transport is injected so tests run offline.
What: ``Provider.execute(task) -> RoleResultLike`` is the uniform seam; remote
adapters subclass it; ``register('name')`` puts a class in the registry and
``get_provider('name')`` returns a singleton instance (or raises a clear error
for an unknown name).
Test: register a dummy provider, get_provider returns it; get_provider('bogus')
raises ValueError naming the providers available.

Layering: this package is imported BY ``role_worker`` for dispatch, so it must
NOT import ``role_worker`` (no ``from tests.harness import role_worker``). The
RoleTask it consumes and the RoleResult it returns are duck-typed dataclasses
(``RoleTaskLike`` / ``RoleResultLike``) so the contract is shared by shape, not
by a back-edge import.
"""
from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


# A transport is a pure function of (url, method, headers, body) -> (status,
# text). Adapters depend ONLY on this signature, never on urllib directly, so a
# fake transport in a test fully replaces the network.
Transport = Callable[..., "tuple[int, str]"]


@dataclass
class RoleTaskLike:
    """The subset of the engine's RoleTask an adapter reads. role_worker's real
    ``RoleTask`` is a structural superset, so its instances satisfy this shape
    without importing it here (layering)."""
    role: str
    node: str
    title: str = ""
    spec: str = ""
    workspace: Optional[str] = None
    specialty: str = ""
    provider: str = ""
    model: str = ""
    params: Optional[dict] = None
    skill: Optional[str] = None
    context: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)


@dataclass
class RoleResultLike:
    """What every remote adapter returns; role_worker adapts it back into its
    own RoleResult / reply shape. ``artifacts`` maps repo-relative path → text."""
    kind: str = "files"
    artifacts: Dict[str, str] = field(default_factory=dict)
    verdict: Optional[dict] = None
    meta: dict = field(default_factory=dict)


def urllib_transport(url: str, *, method: str = "GET",
                     headers: Optional[dict] = None,
                     body: Optional[bytes] = None,
                     timeout: float = 30.0) -> "tuple[int, str]":
    """Default transport: a stdlib urllib round-trip returning (status, text).

    Why: adapters need a real network path for live runs but must stay testable;
    this is the single place that touches the socket, and it is swapped out in
    every test.
    What: issues one HTTP request and returns the status code and decoded body
    (errors surface their HTTP body so the adapter can report them).
    Test: not exercised in the suite (offline); adapters inject a fake instead."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:                            # noqa: PERF203
        return exc.code, exc.read().decode("utf-8", "replace")


def json_request(transport: Transport, url: str, *, method: str,
                 payload: Optional[dict] = None,
                 headers: Optional[dict] = None) -> "tuple[int, Any]":
    """POST/GET JSON via ``transport`` and parse a JSON reply.

    Why: every remote adapter speaks JSON over HTTP; centralising the encode/
    decode keeps each adapter to its own request-shaping logic.
    What: serialises ``payload`` (when given), sets the JSON content-type, and
    returns ``(status, parsed_body_or_raw_text)``.
    Test: drive with a fake transport asserting the url/method/body it received
    and returning a canned ``(200, json_text)``."""
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, text = transport(url, method=method, headers=hdrs, body=body)
    try:
        return status, json.loads(text) if text else {}
    except (ValueError, TypeError):
        return status, text


class Provider(ABC):
    """The uniform executor every role resolves to (doc §3).

    Why: local LLM, Hermes agent, A2A service and MC crew differ only in HOW
    they run a unit of work — behind this ABC the engine treats them identically.
    What: ``execute(task)`` runs ONE RoleTask and returns a RoleResultLike.
    Test: each concrete adapter is tested by building the expected request from a
    RoleTask (fake transport asserts url/method/payload) and parsing a canned
    response into RoleResultLike.artifacts/verdict."""

    name: str = ""

    def __init__(self, transport: Optional[Transport] = None) -> None:
        self.transport: Transport = transport or urllib_transport

    @abstractmethod
    def execute(self, task: RoleTaskLike) -> RoleResultLike:
        """Run one RoleTask, return its RoleResult. Adapters override this."""
        raise NotImplementedError


_REGISTRY: "dict[str, type[Provider]]" = {}


def register(name: str) -> "Callable[[type[Provider]], type[Provider]]":
    """Class decorator: register a Provider subclass under ``name``.

    Why: name-keyed lookup is the whole dispatch mechanism — a YAML
    ``provider: hermes`` resolves to the class registered as 'hermes'.
    What: records the class and stamps its ``name`` attribute.
    Test: decorate a dummy class, assert get_provider(name) returns it."""
    def _wrap(cls: "type[Provider]") -> "type[Provider]":
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return _wrap


def get_provider(name: str, *, transport: Optional[Transport] = None) -> Provider:
    """Return an adapter instance for ``name`` (or raise for an unknown one).

    Why: the single resolution point the call path uses — a bogus provider must
    fail LOUDLY here, not be silently skipped.
    What: instantiates the registered class with the (optional) injected
    transport; ValueError lists the known providers when ``name`` is unknown.
    Test: get_provider('hermes') returns a HermesProvider; get_provider('bogus')
    raises ValueError mentioning the available names."""
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(
            f"unknown provider {name!r}; available: {known}")
    return cls(transport=transport)


def provider_names() -> "list[str]":
    """The registered provider names (for diagnostics / dashboards)."""
    return sorted(_REGISTRY)
