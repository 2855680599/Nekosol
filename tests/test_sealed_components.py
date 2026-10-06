"""Byte-exact integrity test for the sealed native-recall components.

Why this test exists
--------------------
``ProductionNativeMemoryResolver._initialise()`` refuses to start (it stays
``UNAVAILABLE``) as soon as any file listed in ``SEALED_SHA256`` does not hash to
its recorded SHA-256, and ``chiyo_bundle/instance.py`` turns that into
``RuntimeError('memory resolver unavailable: ...')`` for any ``memory=True``
profile. The comparison is byte-exact, so the long-term memory feature can be
disabled by a change that looks cosmetic: converting line endings, running a
formatter, or "normalising" a file.

That actually happened once: ``readonly_recall_adapter.py`` was stored with CRLF
and its pin recorded the CRLF bytes, so any LF normalisation of the repository
would have broken the feature. The pin and the file are now both LF; this test
keeps them in agreement.

What it deliberately does NOT do
--------------------------------
No normalisation, no automatic hash fixing, no CRLF/LF tolerance, and no text
decoding of the sealed files: it hashes the raw bytes on disk exactly as the
resolver does.
"""
from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "components/native/memory_runtime_v1/production_resolver.py"
SEALED_DIR = ROOT / "components/native/native_recall"


def declared_pins() -> dict[str, str]:
    """Read SEALED_SHA256 out of production_resolver.py without importing it.

    Importing the resolver would mutate ``sys.path`` and pull in the Native M3
    tree; the declaration itself is a plain literal we can read directly.
    """
    source = RESOLVER.read_bytes().decode("utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "SEALED_SHA256" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("SEALED_SHA256 is not declared in " + str(RESOLVER))


class SealedComponentIntegrityTest(unittest.TestCase):
    def test_sealed_sha256_is_declared_and_non_empty(self) -> None:
        pins = declared_pins()
        self.assertTrue(pins, "SEALED_SHA256 must declare at least one component")
        for name, value in pins.items():
            self.assertIsInstance(name, str, "sealed component names must be strings")
            self.assertRegex(
                value, r"^[0-9a-f]{64}$",
                f"sealed pin for {name} must be a lowercase SHA-256 hex digest")

    def test_every_declared_sealed_component_matches_its_real_bytes(self) -> None:
        pins = declared_pins()
        checked = 0
        for name, expected in sorted(pins.items()):
            with self.subTest(component=name):
                path = SEALED_DIR / name
                self.assertTrue(
                    path.is_file(),
                    f"sealed component is missing:\n  declared: {name}\n  expected at: {path}")
                raw = path.read_bytes()
                actual = hashlib.sha256(raw).hexdigest()
                # CRLF count is reported to make a line-ending drift obvious.
                crlf = raw.count(b"\r\n")
                self.assertEqual(
                    actual,
                    expected,
                    "sealed component drifted from its declared SHA-256:\n"
                    f"  component: {name}\n"
                    f"  path     : {path}\n"
                    f"  expected : {expected}\n"
                    f"  actual   : {actual}\n"
                    f"  bytes    : {len(raw)} (CRLF: {crlf})\n"
                    "Sealed bytes are frozen: a line-ending conversion, formatter run or "
                    "cosmetic edit will disable the memory resolver. If the change is "
                    "intentional, update SEALED_SHA256 in production_resolver.py in the "
                    "same change and re-run the resolver.")
                checked += 1
        self.assertGreater(checked, 0, "no sealed components were checked")


if __name__ == "__main__":
    unittest.main()
