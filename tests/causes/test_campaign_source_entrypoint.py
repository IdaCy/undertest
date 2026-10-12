import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from causes import campaign_source_entrypoint as entry
from causes import campaign_source_relocation as loader


class SourceEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.write('old.py', 'VALUE = 3\n')
        self.physical = self.write('physical.py', 'VALUE = 3\n')
        native = '''import importlib.util, json, sys
KNOWN = {'core': ORIGINAL}
def module(ref, name):
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value
def main():
    if sys.argv[1:] != ['--authorized']:
        raise ValueError('native_authority_required')
    core = module(KNOWN['core'], 'original_native_core')
    print(json.dumps(dict(value=core.VALUE, source=__file__, core=core.__file__, argv=sys.argv)))
'''.replace('ORIGINAL', repr(self.old))
        self.native = self.write('native.py', native)
        self.binding = self.save('binding.json', {
            'schema': 'causes.native_constructor_source_binding.v1',
            'materializer': self.native,
            'relocation': {'original': self.old, 'physical': self.physical},
            'constructors': [self.native],
        })
        loader_ref, entry_ref = entry.pin(loader.__file__), entry.pin(entry.__file__)
        self.descriptor = {
            'schema': entry.SCHEMA, 'source': self.native,
            'source_review': self.save('native_peer.json', {
                'accepted_test_native_source': True, 'source': self.native, 'findings': [],
            }),
            'source_acceptance_flag': 'accepted_test_native_source',
            'entrypoint': entry_ref,
            'entrypoint_review': self.save('entry_peer.json', {
                'accepted_pinned_native_source_entrypoint': True,
                'source': entry_ref, 'loader_source': loader_ref, 'findings': [],
            }),
            'loader_source': loader_ref,
            'loader_review': self.save('loader_peer.json', {
                'accepted_constructor_source_binding_helper': True,
                'source': loader_ref, 'binding': self.binding, 'findings': [],
            }),
            'binding': self.binding, 'strategy': 'module',
            'native_argv': [sys.executable, '-B', self.native['path'], '--authorized'],
        }

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return entry.pin(path)

    def save(self, name, value):
        return self.write(name, json.dumps(value, sort_keys=True))

    def reference(self):
        return self.save('descriptor.json', self.descriptor)

    def test_original_main_uses_exact_copy_and_restores_process_arguments(self):
        Path(self.old['path']).write_text('VALUE = 16\n')
        previous, bytecode = sys.argv, sys.dont_write_bytecode
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            entry.run_native(self.reference())
        value = json.loads(stdout.getvalue())
        self.assertEqual(value['value'], 3)
        self.assertEqual(value['source'], self.native['path'])
        self.assertEqual(value['core'], self.physical['path'])
        self.assertEqual(value['argv'], self.descriptor['native_argv'][2:])
        self.assertIs(sys.argv, previous)
        self.assertEqual(sys.dont_write_bytecode, bytecode)

    def test_native_authority_failure_remains_native_and_restores_arguments(self):
        previous = sys.argv
        self.descriptor['native_argv'][-1] = '--not-authorized'
        with self.assertRaisesRegex(ValueError, 'native_authority_required'):
            entry.run_native(self.reference())
        self.assertIs(sys.argv, previous)

    def test_changed_source_copy_or_peer_refuses_before_main(self):
        for which in ('source', 'physical', 'peer'):
            with self.subTest(which=which):
                original = copy.deepcopy(self.descriptor)
                if which == 'source':
                    self.descriptor['source'] = dict(self.native, sha256='0' * 64)
                elif which == 'physical':
                    Path(self.physical['path']).write_text('VALUE = 4\n')
                else:
                    self.descriptor['entrypoint_review'] = self.save('bad_peer.json', {})
                with self.assertRaises(ValueError):
                    entry.run_native(self.reference())
                self.descriptor = original
                Path(self.physical['path']).write_text('VALUE = 3\n')

    def test_extra_fields_or_undeclared_constructor_refuse(self):
        self.descriptor['allow_any_source'] = True
        with self.assertRaisesRegex(ValueError, 'exact_entrypoint_descriptor'):
            entry.configuration(self.reference())
        del self.descriptor['allow_any_source']
        self.descriptor['strategy'] = 'arbitrary_factory'
        with self.assertRaisesRegex(ValueError, 'constructor_strategy'):
            entry.configuration(self.reference())

    def test_process_argv_is_explicitly_distinct_from_native_argv(self):
        reference = self.reference()
        actual = entry.process_argv(reference, self.descriptor)
        self.assertEqual(actual[:3], [sys.executable, '-B', entry.__file__])
        self.assertEqual(json.loads(actual[-1]), reference)
        self.assertNotEqual(actual, self.descriptor['native_argv'])

    def test_saved_parent_factory_preserves_cached_original_parent(self):
        source = '''import importlib.util
PARENT = REFERENCE
_PARENT = None
def known():
    return {'input_parent': PARENT}
def parent():
    global _PARENT
    if _PARENT is None:
        spec = importlib.util.spec_from_file_location('original_parent', PARENT['path'])
        _PARENT = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_PARENT)
    return _PARENT
def main():
    return parent().main()
'''.replace('REFERENCE', repr(self.native))
        fresh = self.write('fresh.py', source)
        self.descriptor.update(source=fresh, strategy='saved_parent', source_review=self.save(
            'fresh_peer.json', {'accepted_test_native_source': True, 'source': fresh}))
        self.descriptor['native_argv'][2] = fresh['path']
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            entry.run_native(self.reference())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['value'], 3)
        self.assertEqual(result['argv'][0], fresh['path'])
        self.assertEqual(result['source'], self.native['path'])


if __name__ == '__main__':
    unittest.main()
