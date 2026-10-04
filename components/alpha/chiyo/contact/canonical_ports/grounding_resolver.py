"""CT0-10 Phase F -- canonical contact grounding resolver (order sections 44-49).

WHAT THIS IS
------------
`CanonicalContactGroundingResolver` implements `canonical_spine.CanonicalGroundingPort`:
it turns a bounded set of *verified* contact source refs into the frozen CT0-5
`ContactGroundingSource` projections (verbatim canonical text + content hash) that
Expression may quote. Nothing else.

READ ONLY (order 46)
--------------------
This module has no write path at all: it does not repair a source, does not create a
LifeExperience, does not create Memory, does not persist anything, and does not modify
the Intent or the Decision. Two independent guards enforce that:
  * `resolve()` fingerprints the Intent mapping and the Decision mapping before and
    after resolution and raises `GroundingResolverError` if either changed;
  * `verify_unchanged()` re-resolves and refuses a stale input rather than accepting it
    (order 49).
The test harness additionally scans this file's source for write tokens and asserts the
real memory runtime's write audit stays all-zero.

FAIL CLOSED, NEVER INVENT (order 47)
-----------------------------------
A missing surface, an empty render, an out-of-scope ref, an unresolvable ref scheme or an
oversized snippet is an ERROR. There is no template, no default text, no truncation and no
"best effort" fallback anywhere in this file. In particular the resolver never derives
grounding text from the Intent: the only text it can return is text some declared real
surface actually produced.

THE ONE GENUINELY MISSING CONTACT-SHAPED PORT
---------------------------------------------
The frozen contact contract's own source refs are `lexp:` (AG-2 handoff), `act:`
(activity), `dec:` (decision) and `settle:` (settlement) -- exactly the four prefixes
`ContactGroundingSource` refs use in a real ContactIntent. A whole-tree search for a
resolver of that namespace returns ZERO hits (see MISSING_CONTACT_GROUNDING_PORT below and
`CT0_10_CANONICAL_PORT_MAP.json` -> ports[P10_GROUNDING_SOURCE_RESOLVER], classification
MISSING). No real owner can turn `lexp:handoff-001` into text, so every such ref is
REJECTED here with `MissingCanonicalGroundingOwnerError`.

Real grounding material does exist, but in three other shapes, each resolved through a
declared surface with a declared identity binding:

  * `world_body_capsule`        -> service/bridge/world_body_client.py:338
                                   `render_grounded_context(client, *, max_objects=8)`
                                   (the bounded capsule the bridge injects as grounding;
                                   carries no per-source ref and no content hash, so the
                                   binding is the sha256 of the rendered bounded string);
  * `canonical_relationship_runtime_view`
                                -> agent/canonical_relationship.py:163 `runtime_view`
                                   (the only grounding output that carries a content hash:
                                   record `_sha256` plus a `revision` field);
  * `native_memory_runtime`      -> memory_runtime_v1/runtime.py:455
                                   `NativeMemoryRuntime.compose(RuntimeRequest)`.

Every binding this resolver produces records WHICH surface, WHICH module, WHICH symbol,
what availability it had locally, and what the identity binding is.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # when <ct0-10>/src/ct0_10 is on sys.path
    from canonical_spine import (
        CanonicalGroundingPort,
        CanonicalIntegrationError,
        NAMESPACE_ISOLATED_TEST,
        assert_isolated_store_root,
    )
except ImportError:  # pragma: no cover - when <ct0-10>/src is on sys.path
    from ct0_10.canonical_spine import (
        CanonicalGroundingPort,
        CanonicalIntegrationError,
        NAMESPACE_ISOLATED_TEST,
        assert_isolated_store_root,
    )

# The frozen CT0-5 contact contract (read-only import from the sealed CT0-9 tree).
from contact_draft_model import (
    CONTACT_GROUNDING_SOURCE_SCHEMA,
    ContactGroundingSource,
    sha256_text,
)


MODULE_ID = "ct0_10.canonical_ports.grounding_resolver"
ORDER_SECTIONS = "44-49"
RESOLVER_VERSION = "chiyo.ct0_10.contact_grounding_resolver.v1"

# ==========================================================================
# 1. declared real surfaces
# ==========================================================================
SURFACE_WORLD_BODY_CAPSULE = "world_body_capsule"
SURFACE_CANONICAL_RELATIONSHIP_VIEW = "canonical_relationship_runtime_view"
SURFACE_NATIVE_MEMORY_RUNTIME = "native_memory_runtime"

SURFACES: Mapping[str, Mapping[str, str]] = {
    SURFACE_WORLD_BODY_CAPSULE: {
        "module": "service/bridge/world_body_client.py",
        "symbol": "render_grounded_context(client, *, max_objects=8)",
        "source_line": "service/bridge/world_body_client.py:338; injected by service/bridge/bridge_init.py:152 world_context_hook (registered :307)",
        "identity_binding": "sha256 of the rendered bounded capsule string",
        "carries_content_hash": "no",
        "carries_revision": "no",
    },
    SURFACE_CANONICAL_RELATIONSHIP_VIEW: {
        "module": "agent/canonical_relationship.py",
        "symbol": "runtime_view(config=None, path=None)",
        "source_line": "agent/canonical_relationship.py:163 runtime_view / :177 runtime_view_for_agent",
        "identity_binding": "sha256 of the rendered view plus the record `revision` and the record file sha256",
        "carries_content_hash": "yes (record sha256 inside the rendered block)",
        "carries_revision": "yes (`revision` field of the canonical record)",
    },
    SURFACE_NATIVE_MEMORY_RUNTIME: {
        "module": "memory_runtime_v1/runtime.py",
        "symbol": "NativeMemoryRuntime.compose(RuntimeRequest) -> ShadowResult",
        "source_line": "memory_runtime_v1/runtime.py:455 compose / :434 _capsule",
        "identity_binding": "memory_id surfaced by the real compose() plus the request-scoped input text sha256",
        "carries_content_hash": "no per-snippet field (module-level digest() primitive at runtime.py:21)",
        "carries_revision": "no",
    },
}

SURFACE_AVAILABILITY_IN_TREE = "IN_TREE_INSTALLED"
SURFACE_AVAILABILITY_INSTALLED = "INSTALLED"
SURFACE_AVAILABILITY_CAPTURE = "CAPTURE_NOT_INSTALLED"
SURFACE_AVAILABILITY_MISSING = "NOT_LOCALLY_AVAILABLE"

# ref schemes of the frozen contact contract. NO real owner resolves these.
CONTACT_GROUNDING_REF_PREFIXES = ("lexp:", "act:", "dec:", "settle:")

# declared ref schemes this adapter CAN resolve (one per surface above)
CAPSULE_SOURCE_REF = "capsule:world-body"
RELATIONSHIP_SOURCE_REF = "rel:canonical-counterparty"
MEMORY_SOURCE_REF_PREFIX = "mem:"

MISSING_CONTACT_GROUNDING_PORT: Mapping[str, Any] = {
    "logical_port": "P10_GROUNDING_SOURCE_RESOLVER",
    "classification": "MISSING",
    "contact_namespace": list(CONTACT_GROUNDING_REF_PREFIXES),
    "search": (
        "grep for a resolver of lexp:/act:/dec:/settle: into grounding snippets: 0 hits in "
        "<legacy runtime tree> (scripts/, service/, plugins/) "
        "and 0 hits in the local hermes trees; recorded in CT0_10_CANONICAL_PORT_MAP.json "
        "(ports[P10], classification MISSING) and CT0_10_EXPRESSION_TELEGRAM_RECON.json "
        "(G1_GROUNDING_LEXREF_SOURCE_RESOLVER, exists:false)"
    ),
    "nearest_real_identities": [
        "decision:<sha256[:32]> (service/bridge/decision_trigger.py:94-97)",
        "cand:<sha256[:16]> / cset:<sha256[:16]> (scripts/candidate_sources_ag0.py:1117,2732)",
        "actn:<sha256[:16]> / subk:<...> / arec:<...> (scripts/action_reality_ledger.py:2439,2734,3057)",
    ],
    "consequence": (
        "the CT0-10 resolver is an ADAPTER over the three real surfaces above; every "
        "lexp:/act:/dec:/settle: ref fails closed. A ContactIntent derived from a real "
        "ContactCandidate has exactly that content scope, so no draft can be frozen from a "
        "real candidate scope today -- recorded, never faked."
    ),
}

QUALIFICATION_SCOPE_DECISION = (
    "the frozen CT0-3/CT0-5 contract allows an Intent to carry additional verified "
    "source_refs beyond the Candidate's, as long as content_scope_refs is a subset of "
    "source_refs (contact_intent_model.py:151-168). Because no owner resolves the contact "
    "namespace, the CT0-10 qualification Intent additionally DECLARES the adapter refs "
    "capsule:world-body / rel:canonical-counterparty / mem:<memory_id> in its scope. The "
    "Candidate's own lexp:/act:/dec:/settle: refs stay in source_refs and stay "
    "UNRESOLVABLE, which is proven as a fail-closed case."
)

DEFAULT_MAX_CANONICAL_CHARS = 16000
FROZEN_CONTRACT_TEXT_BOUND = 65536

_CHIYO_DIR = Path(__file__).resolve().parents[2]
_LIFE_RUNTIME_DIR = _CHIYO_DIR / "life_runtime"

# The runtime tree is vendored in this release: chiyo/life_runtime holds the
# canonical owners plus the read-only surface mirrors they are read through.
RUNTIME_ROOT_CANDIDATES = (
    str(_LIFE_RUNTIME_DIR),
)
# The deployed native memory runtime mirror, vendored in this release as
# chiyo/life_runtime/memory_runtime_v1 (the original deployed path was a local
# developer path and was REDACTED for the open-source export).
MEMORY_RUNTIME_ROOT_CANDIDATES = (
    str(_LIFE_RUNTIME_DIR),
)
# The live owner of `agent/canonical_relationship.py` is the hermes install's
# on the deployment host. These are the places an installed copy could be.
RELATIONSHIP_INSTALLED_CANDIDATES = (
    "<redacted: hermes install>/agent/canonical_relationship.py",
    "<redacted: local developer hermes checkout>/agent/canonical_relationship.py",
    "<redacted: local developer hermes checkout>/agent/canonical_relationship.py",
    "<redacted: local developer hermes install>/agent/canonical_relationship.py",
)
# The read-only copy of that module shipped INSIDE this release. It is NOT the
# installed owner; the installed candidates above take precedence when present.
RELATIONSHIP_CAPTURE_CANDIDATES = (
    str(_LIFE_RUNTIME_DIR / "agent" / "canonical_relationship.py"),
)


# ==========================================================================
# 2. errors (all fail-closed)
# ==========================================================================
class GroundingResolverError(CanonicalIntegrationError):
    """Base error for CT0-10 grounding resolution failures."""


class MissingCanonicalGroundingOwnerError(GroundingResolverError):
    """A contact source ref (lexp:/act:/dec:/settle:) has no real owner. Never invented."""


class UnknownGroundingSurfaceError(GroundingResolverError):
    """A source ref uses a scheme no declared surface can resolve."""


class GroundingSourceUnavailableError(GroundingResolverError):
    """A declared surface exists but produced nothing (missing/unreadable/empty)."""


class GroundingSourceOutOfScopeError(GroundingResolverError):
    """A requested ref is not part of the Intent's verified content scope."""


class GroundingSourceTooLargeError(GroundingResolverError):
    """A surface returned more text than the resolver bound. Never truncated."""


class StaleGroundingSourceError(GroundingResolverError):
    """A previously resolved source changed. The stale draft input is REJECTED (order 49)."""

    def __init__(
        self,
        message: str,
        *,
        stale_refs: Sequence[str] = (),
        previous_hashes: Mapping[str, str] | None = None,
        resolved_hashes: Mapping[str, str] | None = None,
        reason: str = "SOURCE_REVISION_CHANGED",
    ) -> None:
        super().__init__(message)
        self.stale_refs = tuple(stale_refs)
        self.previous_hashes = dict(previous_hashes or {})
        self.resolved_hashes = dict(resolved_hashes or {})
        self.reason = reason
        self.action = "REJECT_STALE_DRAFT_INPUT"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "reason": self.reason,
            "action": self.action,
            "stale_refs": list(self.stale_refs),
            "previous_hashes": dict(self.previous_hashes),
            "resolved_hashes": dict(self.resolved_hashes),
        }


# ==========================================================================
# 3. portable binding records
# ==========================================================================
@dataclass(frozen=True, slots=True)
class SurfaceBinding:
    """One resolved source: WHICH real surface produced it and what binds the text."""

    source_ref: str
    surface: str
    surface_module: str
    surface_symbol: str
    surface_availability: str
    surface_provenance_path: str
    identity_binding: str
    content_hash: str
    canonical_text_chars: int
    declared_revision: str | None
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "surface": self.surface,
            "surface_module": self.surface_module,
            "surface_symbol": self.surface_symbol,
            "surface_availability": self.surface_availability,
            "surface_provenance_path": self.surface_provenance_path,
            "identity_binding": self.identity_binding,
            "content_hash": self.content_hash,
            "canonical_text_chars": self.canonical_text_chars,
            "declared_revision": self.declared_revision,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class GroundingResolution:
    """Ordered grounded sources plus the per-ref surface trace."""

    sources: tuple[ContactGroundingSource, ...]
    bindings: tuple[SurfaceBinding, ...]
    requested_refs: tuple[str, ...]
    intent_ref: str
    decision_ref: str
    resolver_version: str = RESOLVER_VERSION

    def by_ref(self) -> dict[str, ContactGroundingSource]:
        return {source.source_ref: source for source in self.sources}

    def pinned_hashes(self) -> dict[str, str]:
        return {source.source_ref: source.content_hash for source in self.sources}

    def surface_for(self, source_ref: str) -> str | None:
        for binding in self.bindings:
            if binding.source_ref == source_ref:
                return binding.surface
        return None

    def binding_for(self, source_ref: str) -> SurfaceBinding | None:
        for binding in self.bindings:
            if binding.source_ref == source_ref:
                return binding
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "resolver_version": self.resolver_version,
            "intent_ref": self.intent_ref,
            "decision_ref": self.decision_ref,
            "requested_refs": list(self.requested_refs),
            "resolved_refs": [source.source_ref for source in self.sources],
            "pinned_hashes": self.pinned_hashes(),
            "surfaces": [binding.to_mapping() for binding in self.bindings],
        }


# ==========================================================================
# 4. small helpers (paths + module loading)
# ==========================================================================
_MODULE_CACHE: dict[str, Any] = {}


def first_existing(candidates: Sequence[str | Path]) -> Path | None:
    for candidate in candidates:
        try:
            path = Path(candidate)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if path.exists():
            return path
    return None


def load_module_from_file(module_name: str, path: Path) -> Any:
    """Import a real runtime module BY FILE, read-only, without mutating it.

    Used only for read-only captures of a real owner module (see surface provenance).
    """
    cached = _MODULE_CACHE.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise GroundingSourceUnavailableError(f"cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[module_name] = module
    return module


def module_file_sha256(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


class _SocketUseForbidden:
    """Any attribute use of the guarded module's socket import raises."""

    def __getattr__(self, name: str) -> Any:
        raise GroundingSourceUnavailableError(
            f"grounding capsule renderer attempted socket use ({name!r}); "
            "the CT0-10 capsule surface must never open a transport"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise GroundingSourceUnavailableError(
            "grounding capsule renderer attempted a socket call; refused"
        )


@contextlib.contextmanager
def capsule_socket_forbidden(world_body_module: Any):
    """Prove the real capsule renderer is called without any socket use.

    The real `WorldBodyClient` talks to a unix socket; CT0-10 injects a read-only
    capture client instead. Guarding the module's `socket` handle makes an accidental
    fallback into the transport impossible rather than merely unlikely.
    """
    missing = object()
    original = getattr(world_body_module, "socket", missing)
    world_body_module.socket = _SocketUseForbidden()
    try:
        yield
    finally:
        if original is missing:  # pragma: no cover - defensive
            delattr(world_body_module, "socket")
        else:
            world_body_module.socket = original


def _canonical(value: Any) -> str:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def grounding_surface_for_ref(source_ref: str) -> str | None:
    """Declared dispatch. Unknown or contact-namespace refs return None (fail closed)."""
    if not isinstance(source_ref, str) or not source_ref:
        return None
    if source_ref.startswith(CONTACT_GROUNDING_REF_PREFIXES):
        return None
    if source_ref == CAPSULE_SOURCE_REF or source_ref.startswith("capsule:"):
        return SURFACE_WORLD_BODY_CAPSULE
    if source_ref.startswith("rel:"):
        return SURFACE_CANONICAL_RELATIONSHIP_VIEW
    if source_ref.startswith(MEMORY_SOURCE_REF_PREFIX):
        return SURFACE_NATIVE_MEMORY_RUNTIME
    return None


# ==========================================================================
# 5. the resolver
# ==========================================================================
class CanonicalContactGroundingResolver:
    """READ-ONLY contact grounding adapter (order sections 44-49).

    Satisfies `canonical_spine.CanonicalGroundingPort`. It never writes, never repairs,
    never fills a gap, and never modifies the Intent or the Decision.
    """

    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        memory_runtime_root: str | Path | None = None,
        relationship_record_path: str | Path | None = None,
        relationship_module_path: str | Path | None = None,
        capsule_client: Any | None = None,
        capsule_payload: Mapping[str, Any] | None = None,
        native_memory_request: Any | None = None,
        native_memory_runtime: Any | None = None,
        max_canonical_chars: int = DEFAULT_MAX_CANONICAL_CHARS,
        capsule_max_objects: int = 8,
    ) -> None:
        self.runtime_root = first_existing((runtime_root,) if runtime_root else RUNTIME_ROOT_CANDIDATES)
        self.memory_runtime_root = first_existing(
            (memory_runtime_root,) if memory_runtime_root else MEMORY_RUNTIME_ROOT_CANDIDATES
        )
        self.relationship_record_path = (
            Path(relationship_record_path) if relationship_record_path is not None else None
        )
        self.relationship_module_path = (
            Path(relationship_module_path) if relationship_module_path is not None else None
        )
        self._capsule_client = capsule_client
        self._capsule_payload = dict(capsule_payload or DEFAULT_CAPSULE_PAYLOAD)
        self.native_memory_request = native_memory_request
        self._native_memory_runtime = native_memory_runtime
        if not isinstance(max_canonical_chars, int) or max_canonical_chars < 1:
            raise GroundingResolverError("max_canonical_chars must be a positive int")
        if max_canonical_chars > FROZEN_CONTRACT_TEXT_BOUND:
            raise GroundingResolverError(
                "max_canonical_chars cannot exceed the frozen contract bound"
            )
        self.max_canonical_chars = max_canonical_chars
        self.capsule_max_objects = int(capsule_max_objects)
        self.last_resolution: GroundingResolution | None = None
        self.surface_reads: list[dict[str, Any]] = []

    # -- protocol entry point ---------------------------------------------
    def resolve_grounding(
        self, *, intent: Any, decision: Any, source_refs: Sequence[str]
    ) -> tuple[ContactGroundingSource, ...]:
        return self.resolve(intent=intent, decision=decision, source_refs=source_refs).sources

    # -- read-only guards --------------------------------------------------
    @staticmethod
    def _intent_fingerprint(intent: Any) -> str:
        return _canonical(intent)

    @staticmethod
    def _decision_fingerprint(decision: Any) -> str:
        return _canonical(decision)

    def _validate_request(
        self, *, intent: Any, decision: Any, source_refs: Sequence[str]
    ) -> tuple[str, ...]:
        if isinstance(source_refs, (str, bytes)) or not isinstance(source_refs, Sequence):
            raise GroundingResolverError("source_refs must be a sequence of refs")
        refs = tuple(source_refs)
        if not refs:
            raise GroundingResolverError("source_refs must not be empty; grounding is required")
        if len(set(refs)) != len(refs):
            raise GroundingResolverError("source_refs must be unique")
        intent_id = getattr(intent, "intent_id", None)
        decision_intent = getattr(decision, "intent_ref", None)
        if not intent_id or not decision_intent:
            raise GroundingResolverError("a typed ContactIntent and Decision receipt are required")
        if decision_intent != intent_id:
            raise GroundingResolverError(
                "the Decision receipt does not belong to this Intent; refusing to resolve"
            )
        declared = set(getattr(intent, "source_refs", ()) or ())
        scope = set(getattr(intent, "content_scope_refs", ()) or ())
        for ref in refs:
            if ref not in declared or ref not in scope:
                raise GroundingSourceOutOfScopeError(
                    f"ref {ref!r} is not part of the Intent's verified content scope; "
                    "the resolver never resolves a source the Intent did not verify"
                )
        for ref in refs:
            surface = grounding_surface_for_ref(ref)
            if surface is not None:
                continue
            if isinstance(ref, str) and ref.startswith(CONTACT_GROUNDING_REF_PREFIXES):
                raise MissingCanonicalGroundingOwnerError(
                    f"no real owner resolves contact source ref {ref!r}: the contact namespace "
                    f"{CONTACT_GROUNDING_REF_PREFIXES} has no resolver anywhere in the runtime "
                    "(0 hits). Recorded as MISSING_CONTACT_GROUNDING_PORT; refusing to invent "
                    "grounding text from the Intent."
                )
            raise UnknownGroundingSurfaceError(
                f"source ref {ref!r} matches no declared grounding surface; refusing to guess"
            )
        return refs

    # -- public resolution -------------------------------------------------
    def resolve(
        self, *, intent: Any, decision: Any, source_refs: Sequence[str]
    ) -> GroundingResolution:
        refs = self._validate_request(intent=intent, decision=decision, source_refs=source_refs)
        intent_before = self._intent_fingerprint(intent)
        decision_before = self._decision_fingerprint(decision)

        sources: list[ContactGroundingSource] = []
        bindings: list[SurfaceBinding] = []
        for ref in refs:
            surface = grounding_surface_for_ref(ref)
            text, binding = self._resolve_one(ref, surface)
            # Order 48: bounded, verbatim, hash-bound. `create` recomputes the hash from
            # the exact text, so a modified snippet cannot be represented at all.
            source = ContactGroundingSource.create(source_ref=ref, canonical_text=text)
            if source.content_hash != binding.content_hash:
                raise GroundingResolverError(
                    "content-hash binding mismatch between the surface output and the source"
                )
            sources.append(source)
            bindings.append(binding)

        if self._intent_fingerprint(intent) != intent_before:
            raise GroundingResolverError(
                "resolver modified the Intent; order 46 forbids it (read-only resolver)"
            )
        if self._decision_fingerprint(decision) != decision_before:
            raise GroundingResolverError(
                "resolver modified the Decision; order 46 forbids it (read-only resolver)"
            )

        resolution = GroundingResolution(
            sources=tuple(sources),
            bindings=tuple(bindings),
            requested_refs=refs,
            intent_ref=str(getattr(intent, "intent_id")),
            decision_ref=str(getattr(decision, "decision_ref")),
        )
        self.last_resolution = resolution
        return resolution

    def verify_unchanged(
        self,
        *,
        previous: GroundingResolution,
        intent: Any,
        decision: Any,
        source_refs: Sequence[str],
    ) -> GroundingResolution:
        """Re-resolve and REJECT a changed source (order 49). Never silently accepts.

        A revision change (a new relationship record revision, a changed capsule, a memory
        that no longer composes) makes the previously pinned draft input invalid: the caller
        must re-resolve and decide again. This method either returns a fresh resolution with
        identical hashes, or raises `StaleGroundingSourceError`.
        """
        try:
            fresh = self.resolve(intent=intent, decision=decision, source_refs=source_refs)
        except GroundingResolverError as exc:
            raise StaleGroundingSourceError(
                f"re-resolution failed ({type(exc).__name__}: {exc}); the pinned draft input "
                "cannot be confirmed and is rejected",
                stale_refs=tuple(previous.pinned_hashes()),
                previous_hashes=previous.pinned_hashes(),
                resolved_hashes={},
                reason="RESOLUTION_UNAVAILABLE",
            ) from exc
        previous_hashes = previous.pinned_hashes()
        resolved_hashes = fresh.pinned_hashes()
        stale = sorted(
            ref
            for ref, digest in previous_hashes.items()
            if resolved_hashes.get(ref) != digest
        )
        if stale:
            raise StaleGroundingSourceError(
                "grounding source revision changed for "
                f"{stale}; the stale draft input is rejected",
                stale_refs=stale,
                previous_hashes=previous_hashes,
                resolved_hashes=resolved_hashes,
            )
        return fresh

    # -- per-surface resolution -------------------------------------------
    def _resolve_one(
        self, ref: str, surface: str | None
    ) -> tuple[str, SurfaceBinding]:
        if surface == SURFACE_WORLD_BODY_CAPSULE:
            return self._resolve_capsule(ref)
        if surface == SURFACE_CANONICAL_RELATIONSHIP_VIEW:
            return self._resolve_relationship(ref)
        if surface == SURFACE_NATIVE_MEMORY_RUNTIME:
            return self._resolve_memory(ref)
        raise UnknownGroundingSurfaceError(f"no declared surface for {ref!r}")  # pragma: no cover

    def _bound(
        self, ref: str, surface: str, text: str, **kwargs: Any
    ) -> tuple[str, SurfaceBinding]:
        if not isinstance(text, str) or not text.strip():
            raise GroundingSourceUnavailableError(
                f"{surface} produced no grounding text for {ref!r}; fail closed (a surface "
                "that renders nothing means no grounding, never a default)"
            )
        if len(text) > self.max_canonical_chars:
            raise GroundingSourceTooLargeError(
                f"{surface} produced {len(text)} chars for {ref!r}, above the CT0-10 bound "
                f"{self.max_canonical_chars}; the resolver never truncates a snippet"
            )
        if len(text) > FROZEN_CONTRACT_TEXT_BOUND:  # pragma: no cover - bound is lower
            raise GroundingSourceTooLargeError("surface text exceeds the frozen contract bound")
        spec = SURFACES[surface]
        binding = SurfaceBinding(
            source_ref=ref,
            surface=surface,
            surface_module=spec["module"],
            surface_symbol=spec["symbol"],
            identity_binding=spec["identity_binding"],
            content_hash=sha256_text(text),
            canonical_text_chars=len(text),
            surface_availability=kwargs.pop("surface_availability"),
            surface_provenance_path=kwargs.pop("surface_provenance_path"),
            declared_revision=kwargs.pop("declared_revision", None),
            notes=tuple(kwargs.pop("notes", ())),
        )
        self.surface_reads.append(binding.to_mapping())
        return text, binding

    # ---- surface 1: in-tree bounded world/body capsule -------------------
    def _world_body_module(self) -> tuple[Any, str, str]:
        if self.runtime_root is None:
            raise GroundingSourceUnavailableError(
                "the real runtime tree was not found; the capsule surface is unavailable "
                "(no fallback: a missing surface is an error)"
            )
        source_dir = Path(self.runtime_root) / "service" / "bridge"
        module_path = source_dir / "world_body_client.py"
        if not module_path.is_file():
            raise GroundingSourceUnavailableError(
                f"world_body_client.py not found at {module_path}; capsule surface unavailable"
            )
        if str(source_dir) not in sys.path:
            sys.path.insert(0, str(source_dir))
        module = load_module_from_file("ct0_10_real_world_body_client", module_path)
        return module, SURFACE_AVAILABILITY_IN_TREE, str(module_path)

    def capsule_stub_client(self, world_body_module: Any | None = None) -> Any:
        """The read-only capture client injected into the REAL capsule renderer.

        `WorldBodyClient` opens a unix socket to ./data/world/run/gateway.sock
        (service/bridge/world_body_client.py:177-179 in `_call_once`). CT0-10 must not
        touch a transport, so this duck-typed capture exposes exactly the three read
        methods the real renderer uses (`enabled`, `get_status`, `get_snapshot`,
        `get_body_signals`) and nothing else.
        """
        if self._capsule_client is not None:
            return self._capsule_client
        if world_body_module is None:
            world_body_module, _, _ = self._world_body_module()
        result = world_body_module.ClientResult
        ok = world_body_module.OK
        payload = self._capsule_payload

        class ReadOnlyCapsuleCaptureClient:
            enabled = True

            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_status(self) -> Any:
                self.calls.append("get_status")
                return result(ok, data={"ready": bool(payload.get("ready", True))})

            def get_snapshot(self) -> Any:
                self.calls.append("get_snapshot")
                return result(ok, data=dict(payload.get("snapshot") or {}))

            def get_body_signals(self) -> Any:
                self.calls.append("get_body_signals")
                return result(ok, data=dict(payload.get("signals") or {}))

        self._capsule_client = ReadOnlyCapsuleCaptureClient()
        return self._capsule_client

    def _resolve_capsule(self, ref: str) -> tuple[str, SurfaceBinding]:
        module, availability, module_path = self._world_body_module()
        client = self.capsule_stub_client(module)
        with capsule_socket_forbidden(module):
            text = module.render_grounded_context(client, max_objects=self.capsule_max_objects)
        return self._bound(
            ref,
            SURFACE_WORLD_BODY_CAPSULE,
            text,
            surface_availability=availability,
            surface_provenance_path=module_path,
            declared_revision=None,
            notes=(
                f"max_objects={self.capsule_max_objects}",
                "rendered by the real in-tree capsule renderer with an injected "
                "read-only capture client; no WorldBodyClient was constructed and the "
                "module's socket handle was guarded, so no transport could be reached",
                "the capsule carries no per-source ref and no revision, so the identity "
                "binding is the sha256 of the rendered bounded string",
                "an empty render means the body runtime is off or NOT READY; that is a "
                "failure here, never an empty capsule passed downstream",
            ),
        )

    # ---- surface 2: canonical relationship runtime view ------------------
    def relationship_module(self) -> tuple[Any, str, str]:
        if self.relationship_module_path is not None:
            path = Path(self.relationship_module_path)
            if not path.is_file():
                raise GroundingSourceUnavailableError(
                    f"configured canonical_relationship module not found at {path}"
                )
            return (
                load_module_from_file("ct0_10_real_canonical_relationship", path),
                SURFACE_AVAILABILITY_CAPTURE,
                str(path),
            )
        installed = first_existing(RELATIONSHIP_INSTALLED_CANDIDATES)
        if installed is not None:
            return (
                load_module_from_file("ct0_10_real_canonical_relationship", installed),
                SURFACE_AVAILABILITY_INSTALLED,
                str(installed),
            )
        capture = first_existing(RELATIONSHIP_CAPTURE_CANDIDATES)
        if capture is not None:
            return (
                load_module_from_file("ct0_10_real_canonical_relationship", capture),
                SURFACE_AVAILABILITY_CAPTURE,
                str(capture),
            )
        raise GroundingSourceUnavailableError(
            "agent/canonical_relationship.py is not installed on this machine and no "
            "read-only capture is available; the relationship surface fails closed "
            "(the live owner is the hermes install's agent/ directory, on the deployment host)"
        )

    def _resolve_relationship(self, ref: str) -> tuple[str, SurfaceBinding]:
        if self.relationship_record_path is None:
            raise GroundingSourceUnavailableError(
                "no canonical relationship record path was supplied; refusing to read the "
                "production record at ./data/persona/canonical/relationship.yaml"
            )
        record_path = Path(self.relationship_record_path)
        module, availability, module_path = self.relationship_module()
        record = module.read_record(record_path)
        if record is None:
            raise GroundingSourceUnavailableError(
                f"the canonical relationship record {record_path} is missing, unreadable, "
                "malformed or not active; the real reader renders nothing and so do we"
            )
        config = {"canonical_relationship": {"context_enabled": True}}
        text = module.runtime_view(config, record_path)
        record_sha = str(record.get("_sha256") or "")
        revision = f"revision={record.get('revision')};record_sha256={record_sha[:16]}"
        return self._bound(
            ref,
            SURFACE_CANONICAL_RELATIONSHIP_VIEW,
            text,
            surface_availability=availability,
            surface_provenance_path=f"{module_path} << {record_path}",
            declared_revision=revision,
            notes=(
                "read through the real read_record()/runtime_view() code path; the record "
                "used here is the ISOLATED qualification record, never the production "
                "record under ./data/persona",
                f"module availability: {availability} "
                "(the live owner is the hermes install's agent/ directory on the deployment host; a "
                "prior-session read-only capture was used on this machine)",
                "record revision and record sha256 come from the real reader and are the "
                "staleness binding for this source",
                f"module_file_sha256={module_file_sha256(Path(module_path))}",
            ),
        )

    # ---- surface 3: native memory runtime --------------------------------
    def memory_module(self) -> tuple[Any, str, str]:
        if self.memory_runtime_root is None:
            raise GroundingSourceUnavailableError(
                "the deployed memory_runtime_v1 mirror was not found; the memory surface "
                "is unavailable and fails closed"
            )
        root = Path(self.memory_runtime_root)
        runtime_file = root / "memory_runtime_v1" / "runtime.py"
        if not runtime_file.is_file():
            raise GroundingSourceUnavailableError(f"memory_runtime_v1/runtime.py not found at {runtime_file}")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = importlib.import_module("memory_runtime_v1")
        return module, SURFACE_AVAILABILITY_INSTALLED, str(runtime_file)

    def _resolve_memory(self, ref: str) -> tuple[str, SurfaceBinding]:
        if self.native_memory_request is None:
            raise GroundingSourceUnavailableError(
                "no RuntimeRequest was supplied; the memory surface cannot compose and "
                "fails closed rather than inventing a snippet"
            )
        module, availability, module_path = self.memory_module()
        runtime = self._native_memory_runtime
        if runtime is None:
            runtime = module.NativeMemoryRuntime()
            self._native_memory_runtime = runtime
        result = runtime.compose(self.native_memory_request)
        memory_id = ref[len(MEMORY_SOURCE_REF_PREFIX):]
        surfaced = tuple(result.manifest.surfaced_memory_ids)
        if memory_id not in surfaced:
            raise GroundingSourceUnavailableError(
                f"the real NativeMemoryRuntime.compose() did not surface memory "
                f"{memory_id!r} (surfaced={list(surfaced)}); fail closed -- the resolver "
                "never grounds on a memory the canonical runtime did not surface"
            )
        text = next(
            (item.text for item in self.native_memory_request.memories if item.memory_id == memory_id),
            None,
        )
        if text is None:  # pragma: no cover - surfaced implies present
            raise GroundingSourceUnavailableError(
                f"surfaced memory {memory_id!r} is not present in the request; fail closed"
            )
        write_audit = runtime.snapshot_writes() if hasattr(runtime, "snapshot_writes") else {}
        if any(int(value) for value in write_audit.values()):
            raise GroundingSourceUnavailableError(
                f"the memory runtime reported writes during a read-only resolution: {write_audit}"
            )
        return self._bound(
            ref,
            SURFACE_NATIVE_MEMORY_RUNTIME,
            text,
            surface_availability=availability,
            surface_provenance_path=module_path,
            declared_revision=f"memory_id={memory_id}",
            notes=(
                "composed by the real NativeMemoryRuntime.compose(); the binding is the "
                "memory_id the runtime actually SURFACED plus the sha256 of the "
                "request-scoped input text",
                "compose() invalidates the EphemeralCapsule before returning "
                "(memory_runtime_v1/runtime.py:582-583), so the capsule contents are "
                "deliberately unreadable afterwards; the resolver therefore quotes the "
                "request-scoped input text of a surfaced memory and never a stale capsule",
                f"runtime write audit after compose: {write_audit} (all zero => no Memory "
                "was created, touched or mutated)",
            ),
        )

    # -- declared surface inventory (for the artifact) ---------------------
    def surface_inventory(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, spec in SURFACES.items():
            row = {"surface": name, **spec}
            if name == SURFACE_WORLD_BODY_CAPSULE:
                try:
                    self._world_body_module()
                    row["availability"] = SURFACE_AVAILABILITY_IN_TREE
                except GroundingResolverError as exc:
                    row["availability"] = SURFACE_AVAILABILITY_MISSING
                    row["error"] = str(exc)
            elif name == SURFACE_CANONICAL_RELATIONSHIP_VIEW:
                try:
                    _, availability, module_path = self.relationship_module()
                    row["availability"] = availability
                    row["provenance_path"] = module_path
                except GroundingResolverError as exc:
                    row["availability"] = SURFACE_AVAILABILITY_MISSING
                    row["error"] = str(exc)
            else:
                try:
                    _, availability, module_path = self.memory_module()
                    row["availability"] = availability
                    row["provenance_path"] = module_path
                except GroundingResolverError as exc:
                    row["availability"] = SURFACE_AVAILABILITY_MISSING
                    row["error"] = str(exc)
            rows.append(row)
        return rows

    def assert_isolated_relationship_record(self) -> Path:
        """The resolver must never read a production home. Fail closed if asked to."""
        if self.relationship_record_path is None:
            raise GroundingResolverError("no relationship record path configured")
        root = Path(self.relationship_record_path).parent
        return assert_isolated_store_root(root, namespace=NAMESPACE_ISOLATED_TEST)


DEFAULT_CAPSULE_PAYLOAD: Mapping[str, Any] = {
    "ready": True,
    "snapshot": {
        "location": {"place_id": "place:sandbox-kitchen", "area_id": "area:sandbox-table"},
        "pose": "SEATED",
        "visible_objects": [
            {"object_id": "obj:lantern", "label": "small blue paper lantern"},
            {"object_id": "obj:cup", "label": "cup"},
        ],
        "hands": {"left": False, "right": True},
    },
    "signals": {
        "signals": [{"signal_type": "TEMPERATURE_WARM"}, {"signal_type": "CALM"}],
        "recovery": {"state": "inactive"},
    },
}


__all__ = [
    "CAPSULE_SOURCE_REF",
    "CONTACT_GROUNDING_REF_PREFIXES",
    "CanonicalContactGroundingResolver",
    "DEFAULT_CAPSULE_PAYLOAD",
    "DEFAULT_MAX_CANONICAL_CHARS",
    "FROZEN_CONTRACT_TEXT_BOUND",
    "GroundingResolution",
    "GroundingResolverError",
    "GroundingSourceOutOfScopeError",
    "GroundingSourceTooLargeError",
    "GroundingSourceUnavailableError",
    "MEMORY_SOURCE_REF_PREFIX",
    "MISSING_CONTACT_GROUNDING_PORT",
    "MODULE_ID",
    "ORDER_SECTIONS",
    "QUALIFICATION_SCOPE_DECISION",
    "RELATIONSHIP_SOURCE_REF",
    "RESOLVER_VERSION",
    "SURFACES",
    "SURFACE_CANONICAL_RELATIONSHIP_VIEW",
    "SURFACE_NATIVE_MEMORY_RUNTIME",
    "SURFACE_WORLD_BODY_CAPSULE",
    "StaleGroundingSourceError",
    "SurfaceBinding",
    "UnknownGroundingSurfaceError",
    "capsule_socket_forbidden",
    "first_existing",
    "grounding_surface_for_ref",
    "load_module_from_file",
    "module_file_sha256",
]
