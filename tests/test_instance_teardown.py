"""A real ``Instance`` must not leak sqlite connections across its lifecycle.

``Instance.close()`` previously released only the life wrapper and the request
database. The memory resolver built by ``OriginalNativeRuntime`` keeps read
connections open on M0/M1/M2/M3 (``readonly_recall_adapter.ReadOnlyRecallStore``
line ``self.conn = _ro(m3_db)``), and with ``memory=False``
``instance._assemble`` dropped that resolver outright. Both paths leaked, which
is the ``ResourceWarning: unclosed database`` that the acceptance notes recorded.

The test asserts zero such warnings and is meant to be executed with
``-W error::ResourceWarning`` as well, so the leak fails the run outright.

Skipped where the Hermes host is not importable (native Windows): a real
Instance cannot be assembled there, and pretending otherwise would be a fake
PASS. See AUDIT_REPORT.md NYA-AUDIT-003 for that platform's status.
"""
import gc
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hermes_host_available() -> bool:
    try:
        import run_agent  # noqa: F401
        return True
    except Exception:
        return False


HERMES_HOST = _hermes_host_available()


@unittest.skipUnless(HERMES_HOST, "Hermes host unavailable (native Windows: POSIX only)")
class RealInstanceTeardownTest(unittest.TestCase):
    def _environment(self):
        env = dict(os.environ)
        env.setdefault("CHIYO_MODEL_API_KEY", "unused-teardown-key")
        env.setdefault("CHIYO_MODEL", "unused-teardown-model")
        env.setdefault("CHIYO_MODEL_BASE_URL", "https://api.example.invalid/v1")
        return env

    def _resource_warnings(self, exercise):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exercise()
            gc.collect()
        return [str(item.message) for item in caught
                if issubclass(item.category, ResourceWarning)]

    def _assert_no_database_leak(self, memory):
        from chiyo_bundle.instance import Instance
        state = tempfile.mkdtemp(prefix="teardown-")
        inst = Instance(state, owner="teardown", memory=memory,
                        environment=self._environment())

        def exercise():
            for _ in range(50):
                inst.new_session()
            inst.close()
            inst.close()  # must be safe to repeat

        try:
            warnings_seen = self._resource_warnings(exercise)
        finally:
            inst.close()
        leaked = [text for text in warnings_seen if "unclosed database" in text]
        self.assertEqual(leaked, [], "sqlite connections leaked: %r" % (leaked,))
        self.assertTrue(inst._closed)

    def test_new_session_cycles_do_not_leak_with_memory_enabled(self):
        self._assert_no_database_leak(memory=True)

    def test_memory_disabled_does_not_leak_the_discarded_resolver(self):
        self._assert_no_database_leak(memory=False)

    def test_close_releases_the_memory_resolver(self):
        from chiyo_bundle.instance import Instance
        state = tempfile.mkdtemp(prefix="teardown-close-")
        inst = Instance(state, owner="teardown", memory=True,
                        environment=self._environment())
        self.assertEqual(inst.native.m37_resolver.state, "READY")
        inst.close()
        self.assertIsNone(inst.native.m37_resolver)
        self.assertIsNone(inst.requests)

    def test_failed_close_is_not_reachable_twice(self):
        from chiyo_bundle.instance import Instance
        state = tempfile.mkdtemp(prefix="teardown-repeat-")
        inst = Instance(state, owner="teardown", memory=True,
                        environment=self._environment())
        inst.close()
        inst.close()
        inst.close()
        self.assertTrue(inst._closed)


if __name__ == "__main__":
    unittest.main()
