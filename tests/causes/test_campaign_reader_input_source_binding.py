import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from causes import campaign_reader_input_audit as audit


class InputSourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.save('native.json', {'native': True})
        self.entry_source = self.save('entry.json', {'entry': True})
        self.loader_source = self.save('loader.json', {'loader': True})
        self.native_argv = ['/python', '-B', self.source['path'], '--request', '/request']
        self.descriptor = {
            'source': self.source, 'native_argv': self.native_argv,
            'strategy': 'saved_parent', 'entrypoint': self.entry_source,
            'loader_source': self.loader_source,
            'entrypoint_review': self.save('entry_peer.json', {
                'accepted_pinned_native_source_entrypoint': True, 'findings': [],
                'source': self.entry_source, 'loader_source': self.loader_source,
            }),
        }
        self.reference = self.save('binding.json', self.descriptor)
        self.settings = {'source_binding': self.reference}
        self.wrapper_argv = ['/python', '-B', '/entrypoint', '--descriptor', json.dumps(self.reference)]
        self.loader, self.binding = object(), {'declared': 'constructor'}
        self.entry = SimpleNamespace(
            configuration=lambda reference: (audit.ck(reference), self.loader, self.binding),
            process_argv=lambda reference, descriptor: self.wrapper_argv,
        )
        self.module = self.enterContext(mock.patch.object(audit, 'module', return_value=self.entry))

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, sort_keys=True))
        return audit.pin(path)

    def check(self, execution, intent, settings=None):
        return audit.invocation_binding(self.settings if settings is None else settings,
                                        execution, intent, self.native_argv, self.source)

    def wrapped(self):
        value = {'argv': self.wrapper_argv, 'source_binding': self.reference,
                 'native_argv': self.native_argv}
        return copy.deepcopy(value), copy.deepcopy(value)

    def test_direct_historical_invocation_remains_unchanged(self):
        self.assertIsNone(self.check({'argv': self.native_argv}, {}, {}))
        self.module.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'exact_native_invocation'):
            self.check({'argv': self.wrapper_argv}, {}, {})

    def test_offline_binding_does_not_relabel_historical_process(self):
        execution, intent = {'argv': self.native_argv}, {'argv': self.native_argv}
        before = copy.deepcopy((execution, intent))
        self.assertEqual(self.check(execution, intent), (self.descriptor, self.loader, self.binding))
        self.assertEqual((execution, intent), before)

    def test_explicit_wrapper_receipt_and_intent_join_exactly(self):
        execution, intent = self.wrapped()
        self.assertEqual(self.check(execution, intent), (self.descriptor, self.loader, self.binding))

    def test_wrapper_cannot_masquerade_as_original_direct_execution(self):
        execution, intent = self.wrapped()
        with self.assertRaisesRegex(ValueError, 'exact_native_invocation'):
            self.check(execution, intent, {})
        del execution['source_binding']
        with self.assertRaisesRegex(ValueError, 'original_direct_invocation_preserved'):
            self.check(execution, intent)

    def test_changed_wrapper_argv_native_argv_or_intent_refuses(self):
        for target, key, replacement in (
                ('execution', 'argv', self.native_argv),
                ('execution', 'native_argv', self.native_argv + ['--extra']),
                ('intent', 'native_argv', self.native_argv + ['--extra']),
                ('intent', 'source_binding', self.source)):
            with self.subTest(target=target, key=key):
                execution, intent = self.wrapped()
                (execution if target == 'execution' else intent)[key] = replacement
                with self.assertRaisesRegex(ValueError, 'exact_bound_native_invocation'):
                    self.check(execution, intent)

    def test_entrypoint_peer_is_checked_before_code_loading(self):
        self.descriptor['entrypoint_review'] = self.save('bad_peer.json', {
            'accepted_pinned_native_source_entrypoint': True,
            'source': self.source, 'loader_source': self.loader_source,
        })
        self.settings['source_binding'] = self.save('bad_binding.json', self.descriptor)
        with self.assertRaisesRegex(ValueError, 'accepted_source_entrypoint'):
            self.check({'argv': self.native_argv}, {})
        self.module.assert_not_called()

    def test_source_strategy_and_exact_native_request_are_fixed(self):
        for key, value in (('source', self.entry_source), ('strategy', 'module'),
                           ('native_argv', self.native_argv + ['--extra'])):
            with self.subTest(key=key):
                descriptor = dict(self.descriptor, **{key: value})
                self.settings['source_binding'] = self.save('changed_binding.json', descriptor)
                with self.assertRaisesRegex(ValueError, 'exact_input_constructor_binding'):
                    self.check({'argv': self.native_argv}, {})


if __name__ == '__main__':
    unittest.main()
