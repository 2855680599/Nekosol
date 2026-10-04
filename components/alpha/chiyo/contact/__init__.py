"""Vendored Contact pipeline: proactive contact, ISOLATED ONLY in this alpha.

Contents:

* the frozen CT0-9 contact contracts and stores (``contact_*``, ``durable``),
* the CT0-10 canonical adapters (``canonical_ports``) that drive the real
  canonical owners, plus ``canonical_spine`` / ``canonical_chain``,
* ``canonical_chain_driver`` - the CT0-10 canonical-wired integration driver,
* ``support`` - the CT0-9 isolated qualification fixture the driver imports,
* the recording transport (``recording_telegram_transport``) and the in-tree
  ``FakeMessageProvider`` path: the ONLY transport that exists here.

There is no real Telegram send in this tree and none can be added by
configuration: the boundary refuses the production destination and the
submission gate is closed unless a caller constructs a declared test gate.

The modules are shipped FLAT; :mod:`chiyo._vendor_paths` puts this directory
and ``canonical_ports`` on ``sys.path`` and aliases ``ct0.<mod>`` /
``ct0_10.<mod>`` onto the very same module objects, because the original
CT0-10 sources use both spellings and a second module object would break the
``isinstance`` / class-identity contracts.

This ``__init__.py`` exists so the tree is installable as a package; the
supported spelling is the flat import.
"""

from __future__ import annotations

__all__: list[str] = []
