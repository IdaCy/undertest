import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_storage as s
from causes import campaign_storage_runtime as runtime
from tests.causes import test_campaign_storage_runtime as fixtures


class StorageDispatcherHookTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RecordedStorageRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def custom(self, *, real=False):
        source = self.fixture.base / 'custom_dispatcher.py'
        body = (
            '        def admission_paused(self):\n'
            '            return super().admission_paused()\n'
        ) if real else (
            "        controller_id = 'custom_owner'\n"
            '        def __init__(self, **kwargs):\n'
            '            self.kwargs = kwargs\n'
            '            type(self).on_construct(kwargs)\n'
            '        def run(self):\n'
            '            type(self).on_run()\n'
            "            assert Path(self.kwargs['start_barrier_file']).is_file()\n"
            "            assert self.kwargs['free_bytes']() >= 80\n"
            "            return {'status': 'paused', 'claims': 0}\n"
        )
        source.write_text('from pathlib import Path\n'
                          'from causes.campaign_dispatcher import Dispatcher\n'
                          'def make():\n'
                          '    class CustomDispatcher(Dispatcher):\n' + body
                          + '    return CustomDispatcher\n')
        name = '_unregistered_storage_dispatcher_fixture'
        self.assertNotIn(name, sys.modules)
        module = types.ModuleType(name)
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
        cls = module.make()
        if not real:
            cls.on_construct = staticmethod(self.fixture.constructed.append)
            cls.on_run = staticmethod(lambda: self.fixture.started.append(cls.controller_id))
        self.fixture.kwargs.update(dispatcher_class=cls, dispatcher_source=h.pin(source))
        return cls, source

    def run_custom(self, authority):
        real_guard = s.StorageGuard
        def observed_guard(binding, *, report):
            self.assertIsInstance(report, s.GuardJournal)
            return real_guard(binding, lambda _: self.fixture.observation, report)
        with patch.object(s, 'StorageGuard', side_effect=observed_guard):
            return runtime.run_recorded_dispatcher(**self.fixture.kwargs, authority=authority)

    def test_custom_nested_class_is_bound_and_keeps_storage_and_barrier_guards(self):
        original = runtime.d.Dispatcher
        cls, source = self.custom()
        result = self.run_custom(self.fixture.authority())
        owner = json.loads((self.fixture.records / 'OWNER.json').read_bytes())
        self.assertEqual(owner['binding']['custom_dispatcher'],
                         {'source': h.pin(source), 'class_qualname': cls.__qualname__})
        self.assertEqual(self.fixture.started, ['custom_owner'])
        self.assertIs(runtime.d.Dispatcher, original)
        self.assertIsInstance(self.fixture.constructed[0]['free_bytes'], s.StorageGuard)
        self.assertTrue(Path(self.fixture.options['start_barrier_file']).exists())
        self.assertTrue(h.lock_free(self.fixture.records / 'guard/reporting.lock'))
        self.assertFalse(h.checked(result)['automatic_restart'])

    def test_default_binding_shape_is_unchanged(self):
        self.assertEqual(set(runtime.launch_binding(**self.fixture.kwargs)), {
            'jobs_sha256', 'config_sha256', 'output', 'storage_binding',
            'record_directory', 'owned_barrier', 'dispatcher_options',
            'runtime_source_sha256', 'storage_source_sha256', 'dispatcher_source_sha256'})

    def test_invalid_or_unbound_custom_implementations_cannot_create_records(self):
        authority = self.fixture.authority()
        cls, source = self.custom()
        for kwargs, reason in [
            ({'dispatcher_class': cls}, 'custom_dispatcher_source_required'),
            ({'dispatcher_source': h.pin(source)}, 'dispatcher_source_requires_custom_class'),
            ({'dispatcher_class': object, 'dispatcher_source': h.pin(source)},
             'custom_dispatcher_subclass_required'),
            ({'dispatcher_class': cls, 'dispatcher_source': h.pin(Path(runtime.__file__))},
             'custom_dispatcher_source_mismatch'),
            ({'dispatcher_class': cls, 'dispatcher_source': h.pin(source)},
             'storage_launch_authority_binding_changed')]:
            with self.subTest(reason=reason):
                self.fixture.kwargs.pop('dispatcher_class', None)
                self.fixture.kwargs.pop('dispatcher_source', None)
                self.fixture.kwargs.update(kwargs)
                with self.assertRaisesRegex(ValueError, reason):
                    self.run_custom(authority)
                self.assertFalse(self.fixture.records.exists())
                self.assertFalse(self.fixture.constructed)

    def test_changed_class_identity_refuses_before_construction(self):
        cls, _ = self.custom()
        authority = self.fixture.authority()
        # A registered module permits source lookup independently of qualname.
        module = types.ModuleType(cls.__module__)
        module.__file__ = self.fixture.kwargs['dispatcher_source']['path']
        with patch.dict(sys.modules, {cls.__module__: module}):
            cls.__qualname__ = 'DifferentDispatcher'
            with self.assertRaisesRegex(ValueError, 'storage_launch_authority_binding_changed'):
                self.run_custom(authority)
        self.assertFalse(self.fixture.records.exists())
        self.assertFalse(self.fixture.constructed)

    def test_changed_custom_source_before_launch_refuses_without_records(self):
        _, source = self.custom()
        authority = self.fixture.authority()
        source.write_text(source.read_text() + '\n')
        with self.assertRaises(ValueError):
            self.run_custom(authority)
        self.assertFalse(self.fixture.records.exists())
        self.assertFalse(self.fixture.constructed)

    def test_custom_source_changed_by_constructor_cannot_enter_run(self):
        cls, source = self.custom()
        authority = self.fixture.authority()
        cls.on_construct = staticmethod(lambda _: source.write_text(source.read_text() + '\n'))
        with self.assertRaises(ValueError):
            self.run_custom(authority)
        failed = json.loads((self.fixture.records / 'FAILED.json').read_bytes())
        self.assertFalse(failed['dispatcher_run_entered'])
        self.assertFalse(self.fixture.started)
        self.assertTrue(h.lock_free(self.fixture.records / 'guard/reporting.lock'))

    def test_user_pause_after_custom_construction_still_prevents_run(self):
        self.custom()
        def pause(count):
            if count == 2:
                Path(self.fixture.options['global_pause_file']).write_text('pause')
        self.fixture.after_policy_check = pause
        with self.assertRaisesRegex(ValueError, 'pause_blocks_handoff'):
            self.run_custom(self.fixture.authority())
        self.assertFalse(self.fixture.started)
        self.assertFalse(json.loads((self.fixture.records / 'FAILED.json').read_bytes())
                         ['dispatcher_run_entered'])
        self.assertTrue(h.lock_free(self.fixture.records / 'guard/reporting.lock'))

    def test_real_nested_dispatcher_waits_at_barrier_without_model_calls(self):
        self.custom(real=True)
        self.fixture.assert_real_dispatcher_waiting(prompt_and_prefix=True)


if __name__ == '__main__':
    unittest.main()
