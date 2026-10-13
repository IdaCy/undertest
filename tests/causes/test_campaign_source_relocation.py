import hashlib
import copy
import importlib.util
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


class ConstructorDependencyTests(SourceFixtures):
    constructor = ConstructorBindingTests.constructor

    def setUp(self):
        super().setUp()
        self.binder, self.binder_ref = self.constructor('binder.py')
        self.root_module, self.root_ref = self.constructor('entry.py')
        self.binder.KNOWN = {'core': self.original}
        self.physical_module = SimpleNamespace(__file__=self.physical['path'])
        self.binder.targets[self.physical['path']] = self.physical_module
        self.binding = dict(materializer=self.binder_ref, relocation=self.declaration)
        self.reader, self.reader_ref = self.constructor('reader.py')
        self.second, self.second_ref = self.constructor('second.py')
        self.actor = self.file('actor.py', b'VALUE = 5\n')
        self.actor_copy = self.file('actor_copy.py', b'VALUE = 5\n')
        self.actor_module = SimpleNamespace(__file__=self.actor_copy['path'])
        self.actor_relocation = dict(original=self.actor, physical=self.actor_copy)
        self.root_module.targets[self.reader_ref['path']] = self.reader
        self.root_module.targets[self.second_ref['path']] = self.second
        for module in (self.reader, self.second):
            module.targets[self.actor_copy['path']] = self.actor_module
            module.targets[self.binder_ref['path']] = self.binder
        self.binding.update(schema='causes.native_constructor_source_binding.v2',
                            constructors=[self.root_ref, self.binder_ref, self.reader_ref, self.second_ref],
                            dependency_relocations=[dict(constructor=ref, relocation=self.actor_relocation)
                                                    for ref in (self.reader_ref, self.second_ref)])

    def bound_reader(self):
        return self.root_module.module(self.reader_ref, 'reader_name')

    def test_nested_two_constructor_copies_and_native_core_keep_logical_pins(self):
        before = copy.deepcopy(self.binding)
        originals = [module.module for module in (self.root_module, self.reader, self.second, self.binder)]
        Path(self.actor['path']).unlink()
        Path(self.original['path']).write_bytes(b'CHANGED = True\n')
        with relocation.bind_constructor(self.root_module, self.root_ref, self.binding) as resolved:
            reader = self.bound_reader()
            self.assertIs(reader.module(self.actor, 'actor_name'), self.actor_module)
            second = self.root_module.module(self.second_ref, 'second_name')
            self.assertIs(second.module(self.actor, 'second_actor_name'), self.actor_module)
            binder = reader.module(self.binder_ref, 'binder_name')
            self.assertIs(binder.module(self.original, 'core_name'), self.physical_module)
        self.assertEqual(self.binding, before)
        self.assertEqual(resolved, [dict(constructor=self.reader_ref, **self.actor_relocation),
                                    dict(constructor=self.second_ref, **self.actor_relocation), self.declaration])
        self.assertEqual(self.reader.calls[0], (self.actor_copy, 'actor_name'))
        self.assertFalse(Path(self.actor['path']).exists())
        for module, original in zip((self.root_module, self.reader, self.second, self.binder), originals):
            self.assertIs(module.module, original)

    def test_unlisted_constructor_and_unrelated_reference_use_original_loader(self):
        sentinel = object()
        self.root_module.targets[self.actor['path']] = sentinel
        other = self.file('other.py', b'VALUE = 5\n')
        self.reader.targets[other['path']] = sentinel
        with relocation.bind_constructor(self.root_module, self.root_ref, self.binding) as resolved:
            self.assertIs(self.root_module.module(self.actor, 'not_relocated'), sentinel)
            self.assertIs(self.bound_reader().module(other, 'unrelated'), sentinel)
        self.assertEqual(resolved, [])
        self.assertEqual(self.root_module.calls[0], (self.actor, 'not_relocated'))

    def test_changed_declared_dependency_pin_refuses_before_native_load(self):
        with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
            reader = self.bound_reader()
            with self.assertRaisesRegex(ValueError, 'dependency_original_mismatch'):
                reader.module(dict(self.actor, sha256='0' * 64), 'changed')
            with self.assertRaisesRegex(ValueError, 'dependency_original_mismatch'):
                reader.module(dict(self.actor, bytes=float(self.actor['bytes'])), 'float_bytes')
            self.assertEqual(reader.calls, [])

    def test_mutating_declaration_does_not_redirect_selected_copy(self):
        other_copy = self.file('unselected_actor_copy.py', b'VALUE = 5\n')
        with relocation.bind_constructor(self.root_module, self.root_ref, self.binding) as resolved:
            self.binding['dependency_relocations'][0]['relocation']['physical'] = other_copy
            self.assertIs(self.bound_reader().module(self.actor, 'actor'), self.actor_module)
        self.assertEqual(resolved[0]['physical'], self.actor_copy)

    def test_bad_schema_scope_or_duplicate_rows_refuse_without_wrapping(self):
        original = self.root_module.module
        mutations = [
            ('extra', lambda b: b.update(ambient=True)),
            ('empty', lambda b: b.update(dependency_relocations=[])),
            ('row_extra', lambda b: b['dependency_relocations'][0].update(ambient=True)),
            ('wrong_constructor', lambda b: b['dependency_relocations'][0]['constructor'].update(sha256='0' * 64)),
            ('undeclared_constructor', lambda b: b['dependency_relocations'][0].update(constructor=self.actor)),
            ('duplicate', lambda b: b['dependency_relocations'].append(copy.deepcopy(b['dependency_relocations'][0]))),
            ('constructor_dependency', lambda b: b['dependency_relocations'][0].update(relocation=dict(original=self.reader_ref, physical=self.file('reader_copy.py', Path(self.reader_ref['path']).read_bytes())))),
            ('core_overlap', lambda b: b['dependency_relocations'][0].update(constructor=self.binder_ref, relocation=self.declaration)),
            ('wrong_bytes', lambda b: b['dependency_relocations'][0]['relocation']['physical'].update(sha256='0' * 64)),
            ('float_bytes', lambda b: b['dependency_relocations'][0]['relocation']['physical'].update(bytes=float(self.actor_copy['bytes']))),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                binding = copy.deepcopy(self.binding)
                mutate(binding)
                with self.assertRaises(ValueError):
                    with relocation.bind_constructor(self.root_module, self.root_ref, binding):
                        self.fail('invalid declaration accepted')
                self.assertIs(self.root_module.module, original)
                self.assertEqual(self.root_module.calls, [])

    def test_v1_does_not_accept_or_apply_v2_rows(self):
        binding = copy.deepcopy(self.binding)
        binding['schema'] = 'causes.native_constructor_source_binding.v1'
        with self.assertRaisesRegex(ValueError, 'constructor_binding_fields'):
            with relocation.bind_constructor(self.root_module, self.root_ref, binding):
                self.fail('v2 rows accepted as v1')
        del binding['dependency_relocations']
        sentinel = object()
        self.reader.targets[self.actor['path']] = sentinel
        with relocation.bind_constructor(self.root_module, self.root_ref, binding) as resolved:
            self.assertIs(self.bound_reader().module(self.actor, 'legacy'), sentinel)
        self.assertEqual(resolved, [])

    def test_copy_changed_during_context_refuses_and_restores_all_loaders(self):
        originals = self.root_module.module, self.reader.module
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                reader = self.bound_reader()
                Path(self.actor_copy['path']).write_bytes(b'VALUE = 6\n')
                reader.module(self.actor, 'actor')
        self.assertIs(self.root_module.module, originals[0])
        self.assertIs(self.reader.module, originals[1])
        self.assertEqual(self.reader.calls, [])

    def test_native_failure_and_tampering_restore_all_nested_loaders(self):
        originals = self.root_module.module, self.reader.module
        for tamper in (False, True):
            with self.subTest(tamper=tamper):
                error, message = (ValueError, 'constructor_binding_changed') if tamper else (RuntimeError, 'native_failure')
                with self.assertRaisesRegex(error, message):
                    with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                        reader = self.bound_reader()
                        reader.module(self.actor, 'actor')
                        if tamper:
                            reader.module = lambda *_: None
                        else:
                            raise RuntimeError('native_failure')
                self.assertIs(self.root_module.module, originals[0])
                self.assertIs(self.reader.module, originals[1])

    def test_false_physical_module_path_refuses_and_restores(self):
        original = self.reader.module
        self.reader.targets[self.actor_copy['path']] = SimpleNamespace(__file__=self.actor['path'])
        with self.assertRaisesRegex(ValueError, 'physical_module_identity'):
            with relocation.bind_constructor(self.root_module, self.root_ref, self.binding):
                self.bound_reader().module(self.actor, 'actor')
        self.assertIs(self.reader.module, original)

    def test_real_native_factory_reads_archive_with_absent_original(self):
        code = '''import hashlib, importlib.util
from pathlib import Path
def module(ref, name):
    data = Path(ref['path']).read_bytes()
    if len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
        raise ValueError('native_pin_changed')
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value
'''
        reader_ref = self.file('native_reader.py', code.encode())
        spec = importlib.util.spec_from_file_location('real_reader', reader_ref['path'])
        reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader)
        binding = dict(self.binding, constructors=[reader_ref, self.binder_ref],
                       dependency_relocations=[dict(constructor=reader_ref, relocation=self.actor_relocation)])
        original = reader.module
        Path(self.actor['path']).unlink()
        with relocation.bind_constructor(reader, reader_ref, binding):
            actor = reader.module(self.actor, 'exact_native_actor_name')
            self.assertEqual(actor.VALUE, 5)
            self.assertEqual(actor.__name__, 'exact_native_actor_name')
            self.assertEqual(actor.__file__, self.actor_copy['path'])
        self.assertIs(reader.module, original)


if __name__ == '__main__':
    unittest.main()
