"""C7 identity envelope and the four non-interchangeable Life Supply identities.

``WriteContext`` (C6) remains the immutable envelope that an owner write records.  PHASE 12
adds the four identities the independent service has to keep apart:

========================= ==========================================================
``HOST_OPERATOR``         the configured operator peer (derived from Unix
                          ``SO_PEERCRED``).  The *only* identity allowed to mint or
                          revoke authority.
``SERVICE_PRINCIPAL``     the service itself (``service:chiyo-life-supply``).  It performs
                          owner writes on behalf of a subject and can never grant
                          itself authority.
``CHIYO_SUBJECT``         the subject whose canonical facts are written.  A subject
                          holds no authority at all.
``OWNER_WRITER``          what the fencing layer sees: owner + writer instance + writer
                          epoch + the per-acquisition writer token.
========================= ==========================================================

The separation is mechanical, not documentary:

* ``assert_separation()`` refuses aliasing between the four canonical names;
* ``require_authority_mint()`` refuses a service principal substituted for the operator;
* ``assert_no_self_authority()`` refuses a request that carries authority-minting fields;
* ``assert_matches_fence()`` refuses an owner-writer identity that is not the live fence's.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

#: Canonical, explicitly named identities.  Never derived from request JSON.
HOST_OPERATOR = "principal:host-operator"
SERVICE_PRINCIPAL = "service:chiyo-life-supply"
CHIYO_SUBJECT = "subject:chiyo"
OWNER_WRITER = "writer:life-supply"

KIND_HOST_OPERATOR = "HOST_OPERATOR"
KIND_SERVICE_PRINCIPAL = "SERVICE_PRINCIPAL"
KIND_CHIYO_SUBJECT = "CHIYO_SUBJECT"
KIND_OWNER_WRITER = "OWNER_WRITER"

IDENTITY_KINDS: tuple[str, ...] = (KIND_HOST_OPERATOR, KIND_SERVICE_PRINCIPAL,
                                   KIND_CHIYO_SUBJECT, KIND_OWNER_WRITER)

#: Request fields that would let a caller mint, borrow, or impersonate authority.  A request
#: carrying any of these is refused outright instead of being ignored or overwritten.
AUTHORITY_MINTING_REQUEST_FIELDS: frozenset[str] = frozenset({
    "grantor", "bootstrap_authorized", "bootstrap", "delegation_allowed",
    "caller_principal", "principal", "identity", "operator", "operator_uid", "is_operator",
    "writer_owner", "writer_instance", "writer_epoch", "writer_token", "actor_uid", "peer_uid",
})


class IdentityError(RuntimeError):
    """An identity is malformed or was used outside its own role."""


class IdentityNotInterchangeable(IdentityError):
    """One identity was substituted for another that it is not."""


class SelfAuthorityRefused(IdentityError):
    """A request tried to supply its own authority rather than have it derived."""


@dataclass(frozen=True)
class WriteContext:
    subject_id: str
    caller_principal: str
    writer_owner: str
    writer_instance: str
    writer_epoch: int
    operation_id: str
    created_at: float

    def validate(self, expected_owner: str) -> None:
        if not all((self.subject_id, self.caller_principal, self.writer_instance, self.operation_id)):
            raise ValueError("identity envelope fields required")
        if self.writer_owner != expected_owner or self.writer_epoch < 1 or self.created_at <= 0:
            raise ValueError("identity envelope owner, epoch, or timestamp invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_context(*, subject_id: str, owner: str, operation_id: str,
                    writer_instance: str, writer_epoch: int, created_at: float,
                    caller_principal: str = SERVICE_PRINCIPAL) -> WriteContext:
    context = WriteContext(subject_id, caller_principal, owner, writer_instance,
                           int(writer_epoch), operation_id, float(created_at))
    context.validate(owner)
    return context


@dataclass(frozen=True)
class Identity:
    """A peer-derived identity: operator peer, service principal, or subject."""

    kind: str
    name: str
    uid: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in IDENTITY_KINDS:
            raise IdentityError(f"unknown identity kind {self.kind!r}")
        if self.kind == KIND_OWNER_WRITER:
            raise IdentityError("owner writer identity requires OwnerWriterIdentity")
        if not self.name:
            raise IdentityError("identity name is required")

    @property
    def mints_authority(self) -> bool:
        return self.kind == KIND_HOST_OPERATOR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OwnerWriterIdentity:
    """The writer identity the fencing layer sees -- never supplied by a caller."""

    owner: str
    instance_id: str
    epoch: int
    writer_token: str
    kind: str = KIND_OWNER_WRITER

    def __post_init__(self) -> None:
        if not self.owner or not self.instance_id or not self.writer_token:
            raise IdentityError("owner writer identity fields are required")
        if int(self.epoch) < 1:
            raise IdentityError("owner writer identity requires a live epoch")

    @property
    def name(self) -> str:
        return f"writer:{self.owner.lower()}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def host_operator(uid: int | None = None, detail: str = "") -> Identity:
    return Identity(KIND_HOST_OPERATOR, HOST_OPERATOR, None if uid is None else int(uid), detail)


def service_principal(uid: int | None = None, detail: str = "") -> Identity:
    return Identity(KIND_SERVICE_PRINCIPAL, SERVICE_PRINCIPAL,
                    None if uid is None else int(uid), detail)


def chiyo_subject(subject_id: str = CHIYO_SUBJECT, uid: int | None = None) -> Identity:
    if not subject_id:
        raise IdentityError("subject identity requires a subject id")
    return Identity(KIND_CHIYO_SUBJECT, f"subject:{subject_id}",
                    None if uid is None else int(uid))


def owner_writer_for_fence(owner: str, fence: Any) -> OwnerWriterIdentity:
    """Derive the owner writer identity from the *live* fence, not from a request."""
    if fence is None:
        raise IdentityError(f"OWNER_WRITER_REQUIRES_FENCE:{owner}")
    epoch = int(fence.assert_current())
    return OwnerWriterIdentity(owner=owner, instance_id=str(fence.instance_id),
                               epoch=epoch, writer_token=str(fence.writer_token))


def assert_matches_fence(identity: OwnerWriterIdentity, fence: Any) -> None:
    """The recorded writer identity must be the one the fencing layer currently holds."""
    if fence is None:
        raise IdentityNotInterchangeable("OWNER_WRITER_REQUIRES_FENCE")
    if (getattr(fence, "owner", None) != identity.owner
            or str(fence.instance_id) != identity.instance_id
            or int(fence.epoch or 0) != int(identity.epoch)
            or str(fence.writer_token) != identity.writer_token):
        raise IdentityNotInterchangeable(
            f"OWNER_WRITER_IDENTITY_MISMATCH:{identity.owner}:"
            f"fence={fence.instance_id}@{fence.epoch} identity={identity.instance_id}@{identity.epoch}")
    fence.assert_current()


def require_authority_mint(identity: Identity | OwnerWriterIdentity) -> Identity:
    """Only the host operator mints authority; nothing else can be substituted for it."""
    if not isinstance(identity, Identity) or identity.kind != KIND_HOST_OPERATOR:
        raise IdentityNotInterchangeable(
            f"AUTHORITY_MINT_REQUIRES_HOST_OPERATOR:got={getattr(identity, 'kind', type(identity).__name__)}")
    return identity


def require_owner_writer(identity: Identity | OwnerWriterIdentity) -> OwnerWriterIdentity:
    if not isinstance(identity, OwnerWriterIdentity):
        raise IdentityNotInterchangeable(
            f"WRITE_REQUIRES_OWNER_WRITER:got={getattr(identity, 'kind', type(identity).__name__)}")
    return identity


def canonical_identities() -> dict[str, str]:
    """The four canonical names, by kind -- used by ``assert_separation`` and tests."""
    return {KIND_HOST_OPERATOR: HOST_OPERATOR, KIND_SERVICE_PRINCIPAL: SERVICE_PRINCIPAL,
            KIND_CHIYO_SUBJECT: CHIYO_SUBJECT, KIND_OWNER_WRITER: OWNER_WRITER}


def assert_separation() -> None:
    """Four kinds, four names: no aliasing, no shared name, no shared value."""
    canonical = canonical_identities()
    if set(canonical) != set(IDENTITY_KINDS):
        raise IdentityNotInterchangeable(f"IDENTITY_KINDS_INCOMPLETE:{sorted(canonical)}")
    names = list(canonical.values())
    if len(set(names)) != len(names):
        raise IdentityNotInterchangeable("IDENTITY_NAMES_ALIASED")
    pairs = ((KIND_HOST_OPERATOR, KIND_SERVICE_PRINCIPAL), (KIND_HOST_OPERATOR, KIND_CHIYO_SUBJECT),
             (KIND_SERVICE_PRINCIPAL, KIND_CHIYO_SUBJECT), (KIND_CHIYO_SUBJECT, KIND_OWNER_WRITER))
    for left, right in pairs:
        if canonical[left] == canonical[right] or canonical[left].startswith(canonical[right]):
            raise IdentityNotInterchangeable(f"IDENTITY_ALIAS:{left}:{right}")


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_keys(item)


def assert_no_self_authority(request: Any) -> None:
    """Refuse a request that carries authority-minting fields anywhere in its payload."""
    offending = sorted({key for key in _walk_keys(request)
                        if key in AUTHORITY_MINTING_REQUEST_FIELDS})
    if offending:
        raise SelfAuthorityRefused(f"SELF_AUTHORITY_REFUSED:{','.join(offending)}")
