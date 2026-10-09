"""Tool authority under personal-memory mode: capability policy + data boundary.

The gate runs as a Hermes ``pre_tool_call`` hook, so it only ever sees
``(tool_name, args, session_id, ...)``. These tests pin the resulting policy:
authorise by capability, deny by default, and refuse outbound arguments that
carry injected memory back out.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor/hermes')]
from chiyo_bundle import hermes_plugin as hp  # noqa: E402
from chiyo_bundle.hermes_plugin import memory_tool_gate  # noqa: E402

RESTRICTED = {'memory': True, 'memory_tool_policy': 'restricted'}
SECRET = '记住一件事：我最喜欢的饮料是冰美式。'


def blocked(result):
    return isinstance(result, dict) and result.get('action') == 'block'


class ToolCapabilityPolicyTests(unittest.TestCase):
    def gate(self, tool, args=None, cfg=None, **kwargs):
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, cfg or RESTRICTED)):
            return memory_tool_gate(tool, args if args is not None else {}, **kwargs)

    def test_outbound_search_tools_are_allowed(self):
        """The user asking for a lookup must work: this is the reported bug."""
        for tool in ('web_search', 'web_extract', 'x_search'):
            self.assertIsNone(self.gate(tool, {'query': 'nyairo.com'}), tool)

    def test_side_effect_free_tools_are_allowed(self):
        for tool in ('todo_list', 'clarify'):
            self.assertIsNone(self.gate(tool), tool)

    def test_tools_that_can_read_private_state_are_denied(self):
        for tool in ('memory', 'session_search', 'terminal', 'process_manage', 'read_file',
                     'write_file', 'patch', 'search_files', 'execute_code', 'delegate_task',
                     'browser_navigate', 'browser_snapshot', 'computer_use', 'read_terminal',
                     'kanban_list', 'kanban_show', 'cronjob_manage', 'a2a_call', 'a2a_orchestrate',
                     'skill_view', 'skills_list', 'vision_analyze', 'video_analyze', 'discord',
                     'discord_admin', 'feishu_doc_read', 'feishu_drive_list_comments',
                     'yb_send_dm', 'ha_get_state', 'spotify_search'):
            self.assertTrue(blocked(self.gate(tool)), tool)

    def test_unknown_and_missing_tools_are_denied_not_guessed(self):
        for tool in ('unknown_future_tool', 'web_search_v2', '', None):
            self.assertTrue(blocked(self.gate(tool)), repr(tool))

    def test_deny_is_registry_derived_not_a_tool_name_allowlist(self):
        """A tool whose *name* looks like search but is not in the public-network
        class must still be denied, and the decision must come from the registry."""
        self.assertTrue(blocked(self.gate('search_files')))
        import model_tools
        self.assertEqual(model_tools.TOOL_TO_TOOLSET_MAP.get('web_search'), 'web')
        self.assertEqual(model_tools.TOOL_TO_TOOLSET_MAP.get('search_files'), 'file')

    def test_memory_tools_stay_denied_even_when_unrestricted(self):
        for tool in ('memory', 'session_search'):
            self.assertTrue(blocked(self.gate(tool, cfg={'memory': True,
                                                         'memory_tool_policy': 'unrestricted'})), tool)

    def test_unrestricted_profile_opens_the_rest(self):
        cfg = {'memory': True, 'memory_tool_policy': 'unrestricted'}
        for tool in ('terminal', 'read_file', 'browser_navigate', 'delegate_task'):
            self.assertIsNone(self.gate(tool, cfg=cfg), tool)

    def test_memory_disabled_disables_the_gate(self):
        cfg = {'memory': False}
        for tool in ('session_search', 'terminal', 'memory', 'unknown_future_tool'):
            self.assertIsNone(self.gate(tool, cfg=cfg), tool)

    def test_unreadable_configuration_fails_closed(self):
        with patch('chiyo_bundle.hermes_plugin.configuration', side_effect=ValueError('damaged')):
            for tool in ('web_search', 'terminal'):
                self.assertTrue(blocked(memory_tool_gate(tool, {})), tool)

    def test_every_denial_says_memory_is_not_broken(self):
        """The model relays the block text, so it must never read as 'memory failed'."""
        cases = [('terminal', {}), ('memory', {}), ('unknown_future_tool', {}),
                 ('session_search', {}), ('read_file', {})]
        for tool, args in cases:
            message = self.gate(tool, args).get('message', '')
            self.assertIn('不是长期记忆故障', message, tool)
            for wrong in ('记忆失效', '记忆已经失效', '长期记忆已失效', '必须关闭', '关闭全部隐私'):
                self.assertNotIn(wrong, message, tool)
        with patch('chiyo_bundle.hermes_plugin.configuration', side_effect=ValueError('damaged')):
            self.assertIn('不是长期记忆内容损坏', memory_tool_gate('terminal', {})['message'])


class DataBoundaryTests(unittest.TestCase):
    """Requirement: an outbound call must not carry injected memory with it."""

    def setUp(self):
        with hp._injected_lock:
            hp._injected_by_session.clear()

    def tearDown(self):
        with hp._injected_lock:
            hp._injected_by_session.clear()

    def test_verbatim_memory_in_outbound_args_is_refused(self):
        hp._remember_injected_fragments('s1', [SECRET])
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertTrue(blocked(memory_tool_gate('web_search', {'query': SECRET}, session_id='s1')))
            self.assertTrue(blocked(memory_tool_gate('web_search',
                                                     {'query': 'search: ' + SECRET}, session_id='s1')))
            self.assertIn('不是记忆故障',
                          memory_tool_gate('web_search', {'query': SECRET}, session_id='s1')['message'])

    def test_public_query_still_passes(self):
        hp._remember_injected_fragments('s1', [SECRET])
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertIsNone(memory_tool_gate('web_search', {'query': 'nyairo.com 是什么'}, session_id='s1'))

    def test_fragments_are_session_scoped(self):
        hp._remember_injected_fragments('s1', [SECRET])
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertIsNone(memory_tool_gate('web_search', {'query': SECRET}, session_id='s2'))

    def test_short_fragments_are_not_tracked(self):
        hp._remember_injected_fragments('s1', ['冰美式'])
        self.assertEqual(hp._injected_fragments('s1'), ())
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertIsNone(memory_tool_gate('web_search', {'query': '冰美式'}, session_id='s1'))

    def test_tracking_is_bounded_and_deduplicated(self):
        for _ in range(5):
            hp._remember_injected_fragments('s1', [SECRET])
        self.assertEqual(len(hp._injected_fragments('s1')), 1)
        hp._remember_injected_fragments('s1', ['x' * 40 for _ in range(300)])
        self.assertLessEqual(len(hp._injected_fragments('s1')), hp._INJECTED_MAX_PER_SESSION)

    def test_negative_memory_background_disguised_as_a_user_instruction(self):
        """The attack the requirement names: injected memory reads as if the user
        asked for it, and the model then hands it to an outbound tool."""
        hp._remember_injected_fragments('s1', [SECRET])
        disguised = {'query': '用户要求：把这段发到网上 -> ' + SECRET}
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertTrue(blocked(memory_tool_gate('web_search', disguised, session_id='s1')))
        # and the same text cannot leave through a private-capability tool either
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertTrue(blocked(memory_tool_gate('terminal', {'command': 'curl ' + SECRET},
                                                     session_id='s1')))


class InjectionRecordingTests(unittest.TestCase):
    """The scan is only meaningful if the engine records what it injected."""

    def setUp(self):
        with hp._injected_lock:
            hp._injected_by_session.clear()

    def tearDown(self):
        with hp._injected_lock:
            hp._injected_by_session.clear()

    def test_select_context_records_the_memory_it_injects(self):
        from chiyo_bundle.instance import install_paths
        install_paths()
        recalled = [NS(text=SECRET)]
        projection = NS(controls=0, healthy=True,
                        recalls=lambda objects: (objects, []), active_corrections=lambda: [])
        bridge = Mock()
        bridge.write_user_event.return_value = {'status': 'inserted', 'event_id': 'fixture-event'}
        svc = NS(memory=True, owner='fixture-owner',
                 binding={'conversation_id': 'fixture-conversation'},
                 native=NS(world_body=None, m37_bridge=bridge,
                           m37_resolver=lambda **kwargs: recalled,
                           memory_controls=NS(projection=lambda: projection)),
                 life_wrapper=NS(_dispatcher=NS(submit=lambda event: None)))
        engine = hp.ChiyoContextEngine()
        engine._authorized = True
        engine._service = svc
        engine._platform = 'cli'
        engine._session_id = 'fixture-session'
        messages = [{'role': 'user', 'content': '我之前说过什么？'}]
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            out = engine.select_context(messages, incoming_message=messages[0])
        self.assertIsNotNone(out)
        self.assertIn(SECRET, hp._injected_fragments('fixture-session'))
        self.assertIn(SECRET, out[-1]['content'])
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, RESTRICTED)):
            self.assertTrue(blocked(memory_tool_gate('web_search', {'query': SECRET},
                                                     session_id='fixture-session')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
