"""P3-C: the public surface is NyAiro-branded while the old names keep working.

What this pins down
-------------------
* The four slash commands are registered under ``nyairo_*`` (both the underscore and
  the hyphen spelling the gateway falls back to) **and** under the previous
  ``chiyo_*`` names, which share the same handler so an older guide keeps working.
* Environment configuration resolves ``NYAIRO_XXX`` -> ``CHIYO_XXX`` -> default, with
  ``NYAIRO_XXX`` winning when both are set, without ever mutating ``os.environ``.
* The documented example config and the user-facing documents show the public names;
  a legacy name may only appear as an explicit compatibility mention.
* The compatibility identifiers stay where they are: the profile data directory
  inside ``HERMES_HOME`` is still ``chiyo/``, the plugin id is still ``chiyo``, and the
  persisted M0 evidence origin constants are untouched.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/hermes"),
                str(ROOT / "components/native"), str(ROOT / "components/native/src")]

from chiyo_bundle import env_alias  # noqa: E402


def plugin_commands() -> dict:
    """Register the shipped plugin against a recording context."""
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("p3c_plugin", ROOT / "plugins/chiyo/__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands: dict = {}
    ctx = SimpleNamespace(llm=None, register_context_engine=lambda engine: None,
                          register_command=lambda name, handler, **kw: commands.update({name: (handler, kw)}),
                          register_hook=lambda *args: None, on_unload=lambda callback: None)
    with patch("chiyo_bundle.hermes_plugin.write_status_evidence"):
        module.register(ctx)
    return commands


class CommandNameTest(unittest.TestCase):
    """A: the new names work. B: the old names still work."""

    def setUp(self):
        self.commands = plugin_commands()

    def test_public_names_are_registered_with_help_text(self):
        for name in ("nyairo_memory", "nyairo_status", "nyairo_consider", "nyairo_note"):
            with self.subTest(command=name):
                self.assertIn(name, self.commands)
                handler, keywords = self.commands[name]
                self.assertTrue(callable(handler))
                self.assertTrue(keywords.get("description"))
                self.assertNotIn("legacy", keywords["description"].lower())

    def test_hyphen_spelling_is_registered_too(self):
        for name in ("nyairo_memory", "nyairo_status", "nyairo_consider", "nyairo_note"):
            with self.subTest(command=name):
                self.assertIs(self.commands[name][0],
                              self.commands[name.replace("_", "-")][0])

    def test_legacy_names_still_call_the_same_handler(self):
        for legacy, current in (("chiyo_memory", "nyairo_memory"), ("chiyo_status", "nyairo_status"),
                                ("chiyo_consider", "nyairo_consider"), ("chiyo_note", "nyairo_note")):
            with self.subTest(command=legacy):
                self.assertIs(self.commands[legacy][0], self.commands[current][0])
                self.assertIs(self.commands[legacy.replace("_", "-")][0], self.commands[current][0])
                # the legacy registration says what it aliases
                self.assertIn(current, self.commands[legacy][1]["description"])

    def test_handler_text_points_at_the_public_names(self):
        from types import SimpleNamespace
        from chiyo_bundle import hermes_plugin
        fake_service = SimpleNamespace(native=SimpleNamespace(_life_supply_client=object()))
        with patch("chiyo_bundle.hermes_plugin.configuration", return_value=(ROOT, {"life_supply_socket": "x"})), \
             patch("chiyo_bundle.hermes_plugin.allowed", return_value=True), \
             patch("chiyo_bundle.hermes_plugin.services", return_value=fake_service):
            message = hermes_plugin.note_command("no separator here")
        self.assertIn("/nyairo_note", message)
        self.assertIn("/nyairo_status", message)
        self.assertNotIn("/chiyo_", message)

    def test_memory_gate_text_points_at_the_public_name(self):
        from chiyo_bundle import hermes_plugin
        with patch("chiyo_bundle.hermes_plugin.configuration",
                   return_value=(ROOT, {"memory": True, "memory_tool_policy": "restricted"})):
            gate = hermes_plugin.memory_tool_gate("memory", {})
        self.assertEqual(gate["action"], "block")
        self.assertIn("/nyairo_memory", gate["message"])
        self.assertNotIn("/chiyo_memory", gate["message"])


class EnvironmentAliasTest(unittest.TestCase):
    """C: public name alone. D: legacy name alone. E: public name wins."""

    def test_public_name_alone_is_resolved(self):
        resolved = env_alias.apply({"NYAIRO_MODEL": "public-model"})
        self.assertEqual(resolved["CHIYO_MODEL"], "public-model")
        self.assertEqual(env_alias.resolve("CHIYO_MODEL", {"NYAIRO_MODEL": "public-model"}),
                         "public-model")

    def test_legacy_name_alone_keeps_working(self):
        env = {"CHIYO_MODEL": "legacy-model"}
        self.assertEqual(env_alias.apply(env)["CHIYO_MODEL"], "legacy-model")
        self.assertEqual(env_alias.resolve("CHIYO_MODEL", env), "legacy-model")

    def test_public_name_wins_when_both_are_set(self):
        both = {"NYAIRO_MODEL": "public-model", "CHIYO_MODEL": "legacy-model"}
        self.assertEqual(env_alias.apply(both)["CHIYO_MODEL"], "public-model")
        self.assertEqual(env_alias.resolve("CHIYO_MODEL", both), "public-model")
        self.assertEqual(env_alias.conflicts(both), [("CHIYO_MODEL", "NYAIRO_MODEL")])

    def test_an_empty_public_name_does_not_shadow_the_legacy_one(self):
        env = {"NYAIRO_MODEL": "   ", "CHIYO_MODEL": "legacy-model"}
        self.assertEqual(env_alias.resolve("CHIYO_MODEL", env), "legacy-model")

    def test_apply_is_pure(self):
        env = {"NYAIRO_MODEL": "public-model"}
        before = dict(env)
        env_alias.apply(env)
        self.assertEqual(env, before)
        self.assertNotIn("CHIYO_MODEL", os.environ)

    def test_instance_accepts_the_public_names_without_touching_os_environ(self):
        from chiyo_bundle.instance import Instance
        process_env = dict(os.environ)
        environment = {"NYAIRO_MODEL_API_KEY": "public-key", "NYAIRO_MODEL": "public-model",
                       "NYAIRO_MODEL_BASE_URL": "https://api.example.invalid/v1"}
        with tempfile.TemporaryDirectory() as temp:
            instance = Instance(Path(temp) / "state", owner="p3c-owner", memory=False,
                                environment=environment)
            try:
                # the Instance resolved the public names into its private mapping
                self.assertEqual(instance.environment["CHIYO_MODEL_API_KEY"], "public-key")
                self.assertEqual(instance.environment["CHIYO_MODEL"], "public-model")
                self.assertFalse(instance.memory)
            finally:
                instance.close()
        self.assertEqual(dict(os.environ), process_env)

    def test_worker_socket_dir_honours_the_public_name_then_the_legacy_one(self):
        self._socket_dir_case({"NYAIRO_M0_WRITER_SOCKET_DIR": "public"})
        self._socket_dir_case({"CHIYO_M0_WRITER_SOCKET_DIR": "legacy"})
        self._socket_dir_case({"NYAIRO_M0_WRITER_SOCKET_DIR": "public",
                               "CHIYO_M0_WRITER_SOCKET_DIR": "legacy"})

    def _socket_dir_case(self, settings):
        import m0_writer_worker as mww
        saved = {name: os.environ.pop(name, None)
                 for name in (mww.PUBLIC_SOCKET_DIR_ENV, mww.SOCKET_DIR_ENV)}
        self.addCleanup(self._restore_env, saved)
        directories = {}
        with tempfile.TemporaryDirectory() as temp:
            for label, value in settings.items():
                directories[label] = Path(temp) / ("public" if "NYAIRO" in label else "legacy")
                directories[label].mkdir()
                os.environ[label] = str(directories[label])
            db = Path(temp) / "state" / "memory" / "evidence.sqlite"
            db.parent.mkdir(parents=True)
            chosen = mww.socket_path_for(db, pid=4242)
            expected = directories["NYAIRO_M0_WRITER_SOCKET_DIR"] if "NYAIRO_M0_WRITER_SOCKET_DIR" in settings \
                else directories["CHIYO_M0_WRITER_SOCKET_DIR"]
            self.assertEqual(chosen.parent, expected, settings)

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class PublicDocumentTest(unittest.TestCase):
    """F: the documents and the example config recommend the public names."""

    PUBLIC_FILES = ("README.md", "FEATURES.md", "PRIVACY_REVIEW.md", "KNOWN_ISSUES.md",
                    "docs/USER_GUIDE.zh-CN.md", "website/USER_GUIDE.zh-CN.md",
                    "website/docs-data.js", "website/index.html")

    def test_the_documents_show_the_public_command_names(self):
        for relative in self.PUBLIC_FILES:
            text = (ROOT / relative).read_text(encoding="utf8")
            with self.subTest(document=relative):
                if relative in ("website/index.html",):
                    self.assertIn("/nyairo_status", text)
                    continue
                self.assertTrue(any("/%s" % name in text
                                    for name in ("nyairo_status", "nyairo_memory",
                                                 "nyairo_note", "nyairo_consider")),
                                "%s shows no public command name" % relative)

    def test_a_legacy_command_name_is_only_ever_a_compatibility_mention(self):
        compatibility = ("兼容", "旧", "legacy", "历史")
        for relative in self.PUBLIC_FILES:
            for number, line in enumerate((ROOT / relative).read_text(encoding="utf8").splitlines(), 1):
                if "/chiyo_" not in line:
                    continue
                with self.subTest(document="%s:%d" % (relative, number)):
                    self.assertTrue(any(word in line for word in compatibility),
                                    "%s:%d still recommends a legacy name: %s" % (relative, number, line[:120]))

    def test_example_config_leads_with_the_public_names(self):
        provider = (ROOT / "components/config/provider.env.example").read_text(encoding="utf8")
        defaults = (ROOT / "components/config/defaults.env.example").read_text(encoding="utf8")
        self.assertIn("NYAIRO_MODEL_API_KEY=", provider)
        self.assertIn("NYAIRO_MODEL=", provider)
        self.assertIn("NYAIRO_MODEL_BASE_URL=", provider)
        self.assertIn("NYAIRO_NATIVE_LIFE_ENABLED=false", defaults)
        self.assertIn("NYAIRO_NATIVE_MEMORY_CONTEXT_MODE=OFF", defaults)
        for text in (provider, defaults):
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if "CHIYO_" in stripped:
                    self.assertTrue(stripped.startswith("#"),
                                    "the example config sets a legacy name outside a comment: %s" % stripped)

    def test_the_launcher_maps_public_names_onto_the_legacy_spelling(self):
        launcher = (ROOT / "scripts/hermes.sh").read_text(encoding="utf8")
        self.assertIn("NYAIRO_*", launcher)
        self.assertIn('CHIYO_${_public#NYAIRO_}', launcher)


class CompatibilityIdentifierTest(unittest.TestCase):
    """G: the compatibility identifiers an existing state depends on are untouched."""

    def test_the_profile_data_directory_is_still_chiyo(self):
        from chiyo_bundle import hermes_plugin
        with patch("chiyo_bundle.hermes_plugin.get_hermes_home", return_value=Path("/tmp/p3c-home")):
            home, config = hermes_plugin.configuration()
        self.assertEqual(home, Path("/tmp/p3c-home"))
        self.assertEqual(config, {})
        # the read path resolves <home>/chiyo/config.json
        with tempfile.TemporaryDirectory() as temp:
            payload = Path(temp) / "chiyo"
            payload.mkdir()
            (payload / "config.json").write_text('{"owner": "p3c-owner", "memory": true}', encoding="utf8")
            with patch("chiyo_bundle.hermes_plugin.get_hermes_home", return_value=Path(temp)):
                _, loaded = hermes_plugin.configuration()
        self.assertEqual(loaded, {"owner": "p3c-owner", "memory": True})

    def test_the_plugin_and_engine_ids_are_unchanged(self):
        self.assertTrue((ROOT / "plugins/chiyo/__init__.py").is_file())
        from chiyo_bundle.hermes_plugin import ChiyoContextEngine
        self.assertEqual(ChiyoContextEngine().name, "chiyo")

    def test_persisted_evidence_origin_values_are_unchanged(self):
        from app.evidence import CHIYO_ORIGIN, USER_ORIGIN
        self.assertEqual(CHIYO_ORIGIN, "CHIYO_VISIBLE_OUTPUT")
        self.assertEqual(USER_ORIGIN, "USER_VISIBLE_INPUT")

    def test_setup_profile_still_writes_the_compatibility_path(self):
        source = (ROOT / "scripts/setup_profile.py").read_text(encoding="utf8")
        self.assertIn("settings=home/'chiyo'", source)
        self.assertIn("'engine']='chiyo'", source)
        self.assertNotIn("CHIYO profile", source)


if __name__ == "__main__":
    unittest.main()
