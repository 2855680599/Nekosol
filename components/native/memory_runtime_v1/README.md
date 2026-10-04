# Chiyo Memory Runtime V1 staging/shadow

This package is an independent, Hermes-free staging implementation of the
Memory Runtime V1 contracts. It is deliberately request-scoped and shadow-only
by default:

- `CHIYO_NATIVE_MEMORY_CONTEXT_ENABLED` is fail-closed and defaults to off.
- `RuntimeRequest.shadow=True` never exposes the hypothetical memory context to
  a provider.
- capsules are invalidated before the request result is returned.
- digest and capsule objects cannot enter conversation history or formation.
- no class in this package imports Hermes or mutates the production stores.

The runner produces contract and verification artifacts. Production shadow
collection reads the Hermes state database read-only and stores only counts,
hashes, and provenance metadata in the V1 artifact directory.
