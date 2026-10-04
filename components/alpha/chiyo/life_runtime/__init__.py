"""Vendored Life Runtime: what she is currently engaged in.

The modules in this directory are the REAL canonical owners (LR-2 activity
continuity, LR-3 interruption, LR-4 waiting/agenda, LR-5 progress/completion,
AR-0 action reality, AR-1 result settlement, AG-0 candidate sources, AG-1
decision) plus the AG-2 life integration layer and the read-only runtime
surface mirrors they are read through.

They are shipped FLAT and import each other flat
(``from activity_continuity import ...``), exactly as the upstream runtime
does, so :mod:`chiyo._vendor_paths` puts this directory on ``sys.path`` and
pins every one of these module names to the file inside this tree.

This ``__init__.py`` exists for one reason only: so the tree is installable as
a package.  Import the modules by their flat names (``import
activity_continuity``) - that is the supported spelling, and
``chiyo.life_runtime.<mod>`` is aliased to the very same module object.
"""

from __future__ import annotations

__all__: list[str] = []
