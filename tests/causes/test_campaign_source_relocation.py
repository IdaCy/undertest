import hashlib
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

from causes import campaign_source_relocation as relocation


class SourceFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.original = self.file('mutable.py', b'VALUE = 3\n')
        self.physical = self.file('frozen.py', b'VALUE = 3\n')
        self.declaration = dict(original=self.original, physical=self.physical)

    def file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


class SourceRelocationTests(SourceFixtures):
    def test_exact_copy_with_changed_original_retains_both_identities(self):
        Path(self.original['path']).write_bytes(b'VALUE = 16\n')
        self.assertEqual(relocation.checked_source(self.original, self.declaration), b'VALUE = 3\n')
        self.assertNotEqual(self.original['path'], self.physical['path'])
        self.assertEqual(self.declaration['original'], self.original)

    def test_missing_original_is_not_recreated(self):
        Path(self.original['path']).unlink()
        self.assertEqual(relocation.checked_source(self.original, self.declaration), b'VALUE = 3\n')
        self.assertFalse(Path(self.original['path']).exists())

    def test_different_bytes_cannot_be_substituted(self):
        other = self.file('new.py', b'VALUE = 16\n')
        with self.assertRaisesRegex(ValueError, 'relocation_content_changed'):
            relocation.physical_source(self.original, dict(original=self.original, physical=other))

    def test_changed_physical_copy_refuses(self):
        Path(self.physical['path']).write_bytes(b'VALUE = 4\n')
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            relocation.physical_source(self.original, self.declaration)

    def test_unrelated_logical_pin_refuses(self):
        other = dict(self.original, path=str(self.root / 'another.py'))
        with self.assertRaisesRegex(ValueError, 'relocation_original_mismatch'):
            relocation.physical_source(other, self.declaration)

    def test_symlink_and_same_path_refuse(self):
        link = self.root / 'alias.py'
        link.symlink_to(self.physical['path'])
        with self.assertRaises(ValueError):
            relocation.physical_source(self.original, dict(original=self.original, physical=dict(self.physical, path=str(link))))
        with self.assertRaisesRegex(ValueError, 'explicit_source_relocation_required'):
            relocation.physical_source(self.original, dict(original=self.original, physical=self.original))

    def test_native_context_loading_changes_only_core_reference(self):
        binding_ref = self.file('binder.py', b'# frozen binder\n')
        parent = self.file('parent.py', b'# frozen reader parent\n')
        runner = self.file('runner.py', b'# frozen runner\n')
        reader, watch = object(), object()
        calls = []
        def module(ref, name):
            calls.append((ref, name))
            if ref == parent:
                return SimpleNamespace(native_modules=lambda: dict(reader=reader, watch=watch))
            return SimpleNamespace(__file__=ref['path'])
        binder = SimpleNamespace(__file__=binding_ref['path'], module=module,
                                 KNOWN=dict(parent=parent, runner=runner, core=self.original))
        modules = relocation.context_modules(binder, binding_ref, self.declaration)
        self.assertEqual(calls, [(parent, 'saved_corpus_reader_parent'), (runner, 'saved_corpus_runner'),
                                 (self.physical, 'saved_corpus_supplement')])
        self.assertIs(modules['reader'], reader)
        self.assertIs(modules['watch'], watch)
        self.assertEqual(modules['core'].__file__, self.physical['path'])
        self.assertEqual(binder.KNOWN['core'], self.original)

    def test_wrong_binder_object_refuses_before_module_load(self):
        ref = self.file('binder.py', b'# binder\n')
        binder = SimpleNamespace(__file__=str(self.root / 'different.py'))
        with self.assertRaisesRegex(ValueError, 'context_binding_module_path'):
            relocation.context_modules(binder, ref, self.declaration)


class ConstructorBindingTests(SourceFixtures):
    def setUp(self):
        super().setUp()
        self.binder, self.binder_ref = self.constructor('binder.py')
        self.root_module, self.root_ref = self.constructor('entry.py')
        self.binder.KNOWN = {'core': self.original}
        self.physical_module = SimpleNamespace(__file__=self.physical['path'])
        self.binder.targets[self.physical['path']] = self.physical_module
        self.root_module.targets[self.binder_ref['path']] = self.binder
        self.binding = dict(schema='causes.native_constructor_source_binding.v1',
                            materializer=self.binder_ref, relocation=self.declaration,
                            constructors=[self.binder_ref, self.root_ref])

    def constructor(self, filename):
        code = ('def module(ref, name):\n'
                '    calls.append((dict(ref), name))\n'
                '    return targets[ref["path"]]\n')
        ref = self.file(filename, code.encode())
        module = ModuleType(filename[:-3])
        module.__file__ = ref['path']
        module.calls, module.targets = [], {}
        exec(compile(code, ref['path'], 'exec'), vars(module))
        return module, ref

    def test_unrelated_source_and_import_name_pass_through(self):
        other = self.file('unrelated.py', b'VALUE = 3\n')
        target = object()
        self.binder.targets[other['path']] = target
        with relocation.bind_constructor(self.binder, self.binder_ref, self.binding) as resolved:
            self.assertIs(self.binder.module(other, 'original_name'), target)
        self.assertEqual(self.binder.calls, [(other, 'original_name')])
        self.assertEqual(resolved, [])

    def test_only_exact_core_pin_resolves_and_both_identities_survive(self):
        Path(self.original['path']).write_bytes(b'VALUE = 16\n')
        changed = dict(self.original, sha256='0' * 64)
        sentinel = object()
        self.binder.targets[self.original['path']] = sentinel
        with relocation.bind_constructor(self.binder, self.binder_ref, self.binding) as resolved:
            self.assertIs(self.binder.module(self.original, 'saved_core'), self.physical_module)
            self.assertIs(self.binder.module(changed, 'unrelated_reference'), sentinel)
        self.assertEqual(self.binder.calls, [(self.physical, 'saved_core'),
                                            (changed, 'unrelated_reference')])
        self.assertEqual(resolved, [self.declaration])
        self.assertEqual(self.binder.KNOWN['core'], self.original)
        self.assertEqual(self.physical_module.__file__, self.physical['path'])

    def test_copy_changed_after_binding_refuses_and_restores(self):
        original = self.binder.module
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            with relocation.bind_constructor(self.binder, self.binder_ref, self.binding):
                Path(self.physical['path']).write_bytes(b'VALUE = 4\n')
                self.binder.module(self.original, 'saved_core')
        self.assertIs(self.binder.module, original)
        self.assertEqual(self.binder.calls, [])

    def test_nested_constructor_uses_original_functions_and_restores_both(self):
        root_loader, binder_loader = self.root_module.module, self.binder.module
        with relocation.bind_constructor(self.root_module, self.root_ref, self.binding) as resolved:
            returned = self.root_module.module(self.binder_ref, 'original_binder')
            self.assertIs(returned, self.binder)
            self.assertIs(returned.module(self.original, 'original_core'), self.physical_module)
        self.assertIs(self.root_module.module, root_loader)
        self.assertIs(self.binder.module, binder_loader)
        self.assertEqual(self.root_module.calls, [(self.binder_ref, 'original_binder')])
        self.assertEqual(resolved, [self.declaration])

    def test_failure_after_nested_load_restores_every_loader(self):
        root_loader, binder_loader = self.root_module.module, self.binder.module
        with self.assertRaisesRegex(RuntimeError, 'native_refusal'):
            with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                self.root_module.module(self.binder_ref, 'original_binder')
                raise RuntimeError('native_refusal')
        self.assertIs(self.root_module.module, root_loader)
        self.assertIs(self.binder.module, binder_loader)

    def test_changed_bound_loader_refuses_after_restoring_every_loader(self):
        root_loader, binder_loader = self.root_module.module, self.binder.module
        with self.assertRaisesRegex(ValueError, 'constructor_binding_changed'):
            with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                self.root_module.module(self.binder_ref, 'original_binder')
                self.binder.module = lambda *_: None
        self.assertIs(self.root_module.module, root_loader)
        self.assertIs(self.binder.module, binder_loader)

    def test_wrong_root_or_nested_module_identity_refuses(self):
        with self.assertRaisesRegex(ValueError, 'constructor_source_identity'):
            with relocation.bind_constructor(self.root_module, self.binder_ref, self.binding):
                self.fail('wrong root accepted')
        original = self.root_module.module
        self.root_module.targets[self.binder_ref['path']] = self.root_module
        with self.assertRaisesRegex(ValueError, 'constructor_source_identity'):
            with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                self.root_module.module(self.binder_ref, 'original_binder')
        self.assertIs(self.root_module.module, original)

    def test_changed_native_core_reference_refuses(self):
        self.binder.KNOWN['core'] = dict(self.original, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'native_core_reference_changed'):
            with relocation.bind_constructor(self.binder, self.binder_ref, self.binding):
                self.fail('changed original core reference accepted')


if __name__ == '__main__':
    unittest.main()
