"""CT0-10 canonical port adapters (the only layer allowed to import the real owners).

* :mod:`ag0_adapter` - Phase D / order sections 32-37 (canonical AG-0 candidate port).
* :mod:`ag1_adapter` - Phase E / order sections 38-43 (canonical AG-1 decision port).

The real owners ship in this tree under ``chiyo/life_runtime`` (copied flat) and
are POSIX-only (``import fcntl``); these adapters therefore run under Linux/WSL.
"""
