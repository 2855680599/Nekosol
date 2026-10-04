"""Corrected LR-2 Activity owner identity (CHIYO-PROD-03 section 19).

The defect
----------
The live Life Runtime renders its Activity owner identity in
``releases/v0.1.0/scripts/runtime_identity.py``::

    owner_id = f'LR2:{getattr(store,"namespace","unknown")}:{getattr(cap,"capability_id","unknown")}'

but ``activity_continuity.WriterCapability`` declares only::

    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    _signature: str = field(repr=False)

There is no ``capability_id`` attribute, so the third segment always falls back to
the literal string ``unknown``. That is the entire reason the live identity document
reports ``LR2:canonical:unknown``: the owner is real, the *rendering* is wrong. It is
not evidence of a missing or unbound owner, and it should not be read as one.

The correction
--------------
Derive the capability segment from fields that actually exist. The digest is stable
for the life of a lease and comparable across restarts and across identity documents,
and it exposes no secret: the per-process HMAC signature is deliberately excluded.

This module is the ready-to-apply patch. It is not applied to the production tree by
this work line; see CHIYO_UNIFIED_BUILD_RESULT.md for the deployment decision.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

__all__ = [
    "CAPABILITY_ID_PREFIX",
    "capability_id_of",
    "owner_id_of",
    "ORIGINAL_EXPRESSION",
    "PATCHED_EXPRESSION",
    "describe_patch",
]

CAPABILITY_ID_PREFIX = "cap-"

#: The exact defective expression as it exists in the deployed runtime.
ORIGINAL_EXPRESSION = (
    'owner_id=f\'LR2:{getattr(store,"namespace","unknown")}:'
    '{getattr(cap,"capability_id","unknown")}\''
)

#: The one-line replacement.
PATCHED_EXPRESSION = "owner_id=owner_id_of(store, cap)"


def capability_id_of(capability: Any) -> str:
    """Stable capability identity derived from fields the capability really has.

    Forward compatible: if a future ``WriterCapability`` grows a real
    ``capability_id``, that value wins and this derivation is not used.
    """
    explicit = getattr(capability, "capability_id", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    parts = (
        str(getattr(capability, "writer_domain", "") or ""),
        str(getattr(capability, "namespace", "") or ""),
        str(getattr(capability, "store_root", "") or ""),
        str(getattr(capability, "lease_instance_id", "") or ""),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return CAPABILITY_ID_PREFIX + digest[:16]


def owner_id_of(store: Any, capability: Any, *, fallback_namespace: str = "unknown") -> str:
    """Render the LR-2 owner id with a real third segment."""
    namespace = getattr(store, "namespace", None) or fallback_namespace
    return f"LR2:{namespace}:{capability_id_of(capability)}"


def describe_patch() -> dict[str, Optional[str]]:
    return {
        "file": "releases/v0.1.0/scripts/runtime_identity.py",
        "line": "91",
        "original": ORIGINAL_EXPRESSION,
        "patched": PATCHED_EXPRESSION,
        "requires_import": "from life_owner_identity import owner_id_of",
    }
