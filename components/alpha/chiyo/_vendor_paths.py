"""Runtime import bootstrap for the vendored CHIYO release tree.

The canonical owners (``chiyo/life_runtime``) and the frozen contact stack
(``chiyo/contact``) are shipped **flat**: every module imports only the Python
standard library plus its local siblings (``from activity_continuity import ...``,
``from contact_intent_model import ...``, ``import canonical_spine``).  Two
subtrees nevertheless need to stay importable as packages:

* ``chiyo/contact/canonical_ports`` (``import canonical_ports.ag0_adapter``),
* ``chiyo/contact/durable`` (``from durable.durable_file_ops import ...``),
* ``chiyo/contact/support`` (``from support.ct0_9_isolation_fixtures import ...``).

Importing this module does four things, in this order:

1. puts ``chiyo/life_runtime``, ``chiyo/contact`` and
   ``chiyo/contact/canonical_ports`` on ``sys.path``;
2. pins every vendored flat module name to the file inside this tree, so a
   foreign directory that some adapter appends to ``sys.path`` can never
   shadow it (that would produce *two* module objects and break
   ``isinstance`` / class-identity contracts);
3. installs an alias finder so that ``ct0.<mod>`` and ``ct0_10.<mod>`` resolve
   to the **same module object** as the flat ``<mod>``.  The original CT0-10
   sources use both spellings (``from ct0_10.canonical_spine import ...`` and
   ``from ct0 import contact_intent_agency_bridge``); alias resolution that
   created a second module object is the single most likely way to break the
   class-identity checks in this tree;
4. sets the environment defaults that point every adapter at the vendored
   directories instead of a developer machine's absolute paths.

Nothing here writes to disk, opens a socket, reads a credential or changes a
kill switch.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import types
from pathlib import Path

__all__ = [
    "CANONICAL_PORTS_DIR",
    "CONTACT_DIR",
    "LIFE_RUNTIME_DIR",
    "PACKAGE_DIR",
    "SUPPORT_DIR",
    "TREE_DIR",
    "alias_report",
    "assert_single_module_objects",
    "contact_chain_env",
    "ensure_importable",
]

PACKAGE_DIR = Path(__file__).resolve().parent
TREE_DIR = PACKAGE_DIR.parent
LIFE_RUNTIME_DIR = PACKAGE_DIR / "life_runtime"
CONTACT_DIR = PACKAGE_DIR / "contact"
CANONICAL_PORTS_DIR = CONTACT_DIR / "canonical_ports"
SUPPORT_DIR = CONTACT_DIR / "support"

_FLAT_DIRS = (CONTACT_DIR, LIFE_RUNTIME_DIR, CANONICAL_PORTS_DIR)
#: every spelling that must resolve to ONE module object.  ``chiyo.contact.`` /
#: ``chiyo.life_runtime.`` are the installed-package spellings; ``ct0.`` /
#: ``ct0_10.`` are the spellings the original CT0-10 sources use.
_ALIAS_PREFIXES = ("chiyo.life_runtime.", "chiyo.contact.", "ct0_10.", "ct0.")
_ALIAS_PACKAGES = ("ct0", "ct0_10")

# ---------------------------------------------------------------------------
# 1. sys.path
# ---------------------------------------------------------------------------
for _directory in (LIFE_RUNTIME_DIR, CANONICAL_PORTS_DIR, CONTACT_DIR):
    _entry = str(_directory)
    if _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

# ---------------------------------------------------------------------------
# 2. the set of flat module names shipped inside this tree
# ---------------------------------------------------------------------------
_STDLIB_NAMES = set(getattr(sys, "stdlib_module_names", frozenset()))
_FLAT_NAMES: frozenset[str] = frozenset(
    path.stem
    for _directory in _FLAT_DIRS
    for path in _directory.glob("*.py")
    if path.stem != "__init__" and path.stem not in _STDLIB_NAMES
)


def _flat_target_exists(flat_name: str) -> bool:
    body = flat_name.replace(".", "/")
    for directory in _FLAT_DIRS:
        if (directory / f"{flat_name}.py").is_file():
            return True
        if (directory / f"{body}.py").is_file():
            return True
        if (directory / body).is_dir():
            return True
        if (directory / body / "__init__.py").is_file():
            return True
    return False


class _AliasLoader(importlib.abc.Loader):
    """Serve ``alias`` by importing ``flat`` and returning that very object."""

    def __init__(self, flat_name: str) -> None:
        self.flat_name = flat_name

    def create_module(self, spec):  # noqa: ANN001, ANN201 - importlib protocol
        return importlib.import_module(self.flat_name)

    def exec_module(self, module) -> None:  # noqa: ANN001 - importlib protocol
        return None


class _VendoredFinder(importlib.abc.MetaPathFinder):
    """Pins vendored flat modules and resolves the ``ct0`` / ``ct0_10`` aliases."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        for prefix in _ALIAS_PREFIXES:
            if fullname.startswith(prefix):
                flat_name = fullname[len(prefix):]
                if _flat_target_exists(flat_name):
                    return importlib.machinery.ModuleSpec(
                        fullname, _AliasLoader(flat_name)
                    )
                return None
        if "." not in fullname and fullname in _FLAT_NAMES:
            for directory in _FLAT_DIRS:
                candidate = directory / f"{fullname}.py"
                if candidate.is_file():
                    return importlib.util.spec_from_file_location(
                        fullname, str(candidate)
                    )
        return None


def _install() -> None:
    if not any(isinstance(finder, _VendoredFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _VendoredFinder())
    for package_name in _ALIAS_PACKAGES:
        module = sys.modules.get(package_name)
        if module is None:
            module = types.ModuleType(package_name)
            sys.modules[package_name] = module
        module.__path__ = []  # type: ignore[attr-defined]
        module.__doc__ = (
            f"compatibility alias package: {package_name}.<mod> resolves to the "
            "single vendored module object <mod> in chiyo/contact (see "
            "chiyo/_vendor_paths.py)"
        )


_install()

# ---------------------------------------------------------------------------
# 3. environment defaults: point every adapter at this tree
# ---------------------------------------------------------------------------
# ``setdefault`` on purpose: an operator may still override any of these, but
# nothing silently falls back to a developer machine's absolute path.
_ENV_DEFAULTS = {
    # AG-0 imports the frozen CT0-9 contact contracts for isinstance checks
    "CT0_CONTACT_CONTRACT_ROOT": str(CONTACT_DIR),
    # the canonical runtime root used by load_real_ag0/load_real_ag1 (<root>/scripts)
    "CHIYO_CANONICAL_RUNTIME_ROOT": str(LIFE_RUNTIME_DIR),
    # read_ports / ar0_adapter / ar1_consumer runtime scripts
    "CT0_10_RUNTIME_ROOT": str(LIFE_RUNTIME_DIR),
    "CT0_10_RUNTIME": str(LIFE_RUNTIME_DIR),
    # canonical_chain.py looks for the frozen contact contracts here
    "CT0_9_SANDBOX_SRC": str(CONTACT_DIR),
    # capacity_resolver's frozen-snapshot import
    "CT0_9_CONTACT_SRC": str(SUPPORT_DIR),
    # the contact chain driver derives <sandbox>/src/ct0 and <sandbox>/tests
    "CT0_10_SANDBOX": str(TREE_DIR),
}
for _key, _value in _ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
del _key, _value


# ---------------------------------------------------------------------------
# 4. helpers
# ---------------------------------------------------------------------------
def ensure_importable() -> tuple[Path, Path]:
    """Return ``(life_runtime_dir, contact_dir)`` after asserting the tree is live."""
    if not (LIFE_RUNTIME_DIR / "activity_continuity.py").is_file():
        raise RuntimeError(f"vendored life runtime missing under {LIFE_RUNTIME_DIR}")
    if not (CONTACT_DIR / "canonical_spine.py").is_file():
        raise RuntimeError(f"vendored contact stack missing under {CONTACT_DIR}")
    return LIFE_RUNTIME_DIR, CONTACT_DIR


def contact_chain_env(*, data_dir: str | Path, out_dir: str | Path) -> dict[str, str]:
    """Environment for a child process that runs the vendored contact chain driver."""
    data = Path(data_dir)
    temp = data / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join([str(TREE_DIR), str(CONTACT_DIR)]),
            "TMPDIR": str(temp),
            "TEMP": str(temp),
            "TMP": str(temp),
            "CT0_10_ISO_ROOT": str(data / "iso"),
            "CT0_10_OUT": str(out_dir),
            "CT0_10_RUNTIME": str(LIFE_RUNTIME_DIR),
            "CT0_10_SANDBOX": str(TREE_DIR),
        }
    )
    return env


def alias_report(name: str) -> dict[str, str]:
    """Return the flat/aliased module *object* identity for one module name."""
    flat_name = name
    for prefix in _ALIAS_PREFIXES:
        if name.startswith(prefix):
            flat_name = name[len(prefix):]
            break
    flat = importlib.import_module(flat_name)
    aliased = importlib.import_module(name)
    return {
        "alias_name": name,
        "flat_name": flat_name,
        "same_object": str(flat is aliased),
        "flat_file": str(getattr(flat, "__file__", "?")),
    }


def assert_single_module_objects() -> list[dict[str, object]]:
    """Prove that both spellings of the packaged CT0-10 modules are one object.

    Returns one row per checked class pairing.  Raises ``AssertionError`` when a
    duplicate module object exists (which would silently break ``isinstance``).
    """
    rows: list[dict[str, object]] = []
    pairs = (
        ("ct0_10.canonical_spine", "CanonicalIntegrationError"),
        ("canonical_spine", "CanonicalIntegrationError"),
        ("ct0_10.canonical_ports.ag0_adapter", "CanonicalAg0CandidateAdapter"),
        ("canonical_ports.ag0_adapter", "CanonicalAg0CandidateAdapter"),
        ("ct0_10.canonical_ports.ag1_adapter", "CanonicalAg1DecisionAdapter"),
        ("canonical_ports.ag1_adapter", "CanonicalAg1DecisionAdapter"),
        ("ct0_10.canonical_ports.telegram_boundary", "CanonicalTelegramBoundary"),
        ("canonical_ports.telegram_boundary", "CanonicalTelegramBoundary"),
        ("ct0.contact_intent_agency_bridge", "ContactCapacitySnapshot"),
        ("contact_intent_agency_bridge", "ContactCapacitySnapshot"),
    )
    seen: dict[tuple[str, str], object] = {}
    for module_name, symbol in pairs:
        module = importlib.import_module(module_name)
        obj = getattr(module, symbol)
        key = (module.__name__.split(".")[-1], symbol)
        previous = seen.get(key)
        if previous is not None and previous is not obj:
            raise AssertionError(
                f"duplicate module object for {module_name}.{symbol}: "
                f"{module.__file__!r} is not the object reached through "
                f"{getattr(previous, '__module__', '?')!r}"
            )
        seen[key] = obj
        rows.append(
            {
                "module": module_name,
                "class": symbol,
                "class_id": id(obj),
                "module_file": str(getattr(module, "__file__", "?")),
                "module_object_id": id(module),
            }
        )
    return rows
