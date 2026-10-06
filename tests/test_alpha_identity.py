"""NYA-AUDIT-011: components/alpha is a historical/control tree, not production.

A parallel, already-diverged Life Runtime lives at
``components/alpha/chiyo/life_runtime/`` (with its own ``memory_runtime_v1``)
alongside the live implementation at ``components/life/``. Nothing wires the
alpha tree into a running individual, but that is easy to get wrong: fixing or
testing the wrong lifecycle is a real failure mode.

These tests assert the isolation rather than trusting the documentation:

1. no production module under ``chiyo_bundle/`` or ``plugins/`` refers to the
   alpha tree, statically;
2. no module loaded by ``chiyo_bundle`` at runtime is served from alpha.

Deliberately not asserted: that the strings "alpha" or "beta" never appear.
They are also release-tag identifiers (see ``scripts/update.py``), so a blanket
word check would be both wrong and brittle.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCTION_TREES = (ROOT / "chiyo_bundle", ROOT / "plugins")
ALPHA = ROOT / "components" / "alpha"

# A reference to the alpha *tree*: a path segment, an import of its package, or
# a sys.path injection. Not the bare word "alpha".
REFERENCE_PATTERNS = (
    re.compile(r"components[/\\]+alpha"),
    re.compile(r"\balpha[/\\]+chiyo\b"),
    re.compile(r"^\s*(?:from|import)\s+alpha\b", re.MULTILINE),
    re.compile(r"['\"]alpha['\"]\s*/\s*['\"]chiyo['\"]"),
)


class StaticIsolationTest(unittest.TestCase):
    def test_production_trees_do_not_reference_the_alpha_tree(self):
        findings = []
        scanned = 0
        for tree in PRODUCTION_TREES:
            self.assertTrue(tree.is_dir(), "missing production tree: %s" % tree)
            for path in sorted(tree.rglob("*.py")):
                scanned += 1
                text = path.read_text(encoding="utf8", errors="replace")
                for pattern in REFERENCE_PATTERNS:
                    match = pattern.search(text)
                    if match:
                        findings.append("%s: %r" % (
                            path.relative_to(ROOT).as_posix(), match.group(0)))
        self.assertGreater(scanned, 0, "no production module was scanned")
        self.assertEqual(findings, [],
                         "production code references the alpha tree: %r" % (findings,))


class RuntimeIsolationTest(unittest.TestCase):
    def test_no_module_loaded_by_chiyo_bundle_comes_from_alpha(self):
        import chiyo_bundle.instance  # noqa: F401
        try:
            import chiyo_bundle.presence  # noqa: F401
        except Exception:
            pass
        leaked = []
        for name, module in list(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if not origin:
                continue
            try:
                resolved = Path(origin).resolve()
                resolved.relative_to(ALPHA.resolve())
            except (ValueError, OSError):
                continue
            leaked.append(name)
        self.assertEqual(leaked, [],
                         "these loaded modules come from components/alpha: %r" % (leaked,))

    def test_alpha_tree_still_carries_its_identity_notice(self):
        readme = ALPHA / "README.md"
        self.assertTrue(readme.is_file())
        text = readme.read_text(encoding="utf8")
        self.assertIn("不是生产运行时", text)
        self.assertIn("components/life/", text)


if __name__ == "__main__":
    unittest.main()
