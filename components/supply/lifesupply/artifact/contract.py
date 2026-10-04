"""Versioned reconstruction boundary for ManagedArtifact.

The original frozen B3/B4 contract and SQLite schema were not recovered. This explicit
C5 v1 contract is a proposal derived from the user's handoff, not the recovered original.
It must remain separately versioned and never be represented as the B3 source baseline.
"""
SCHEMA_VERSION = 1
OWNER = "ManagedArtifact"
OUTCOMES = frozenset({"OK", "NO_CHANGE", "DENY", "CONFLICT", "BLOCKED", "NOT_FOUND", "UNKNOWN"})
OPERATIONS = frozenset({"CREATE", "COMMIT_MARKDOWN", "ARCHIVE", "TOMBSTONE"})

FROZEN_REQUIREMENTS = {
    "idempotency_order": "lookup operation key and compare operation digest before CAS",
    "immutable_versions": True,
    "no_change_creates_version": False,
    "effect_receipt": True,
    "projection_divergence_detectable": True,
    "tombstone_is_terminal_for_continuation": True,
    "sqlite_transactional_commit": True,
}

# This contract formalizes C5 input requirements; it is not an original B3 artifact.
