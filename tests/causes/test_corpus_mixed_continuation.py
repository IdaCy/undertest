"""Synthetic mixed-corpus allocation contracts; no provider or task execution."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_monitor as monitor
from causes.campaign_dispatcher import Dispatcher
from causes.corpus_allocation import admissible_cells, digest, key
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_campaign_dispatcher import finish
from tests.causes.test_campaign_monitor import add_continuation, fixture as monitor_fixture
from tests.causes.test_corpus_continuation import fixture, write_control


SELECTION = {'method': 'one_cell_per_task_rotating_v1'}


def physical(job):
    return job['spec']['task_id'], job['variant']['stimulus_sha256']


def full_set(block):
    return {physical(job) for job in block['jobs'] if job['allocation']['continue_to_end']}


def mixed_control(directory, args, *, schema='causes.corpus_continuation_control.v2',
                  selection=SELECTION):
    original = write_control(directory, args)
    path = Path(original['path'])
    value = json.loads(path.read_bytes())
    value.update(schema=schema, full_selection=copy.deepcopy(selection))
    path.write_text(json.dumps(value))
    raw = path.read_bytes()
    import hashlib
    return value, {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def mixed_monitor_fixture(max_full=2):
    """Adapt the existing metadata fixture, repinning every changed object."""
    policy, registration, objects, ref = monitor_fixture()
    add_continuation(policy, registration, objects, ref)
    phase = policy['phases'][-1]
    old_control = phase['continuation']['control']
    control = copy.deepcopy(objects[old_control['path']])
    control.update(schema='causes.corpus_continuation_control.v2',
                   full_selection=copy.deepcopy(SELECTION))
    control_pin = ref(Path(old_control['path']).name, control)
    argv = phase['process']['argv'].copy()
    argv[argv.index('--corpus-continuation-sha256') + 1] = control_pin['sha256']
    argv.extend(('--max-full', str(max_full)))
    phase['process']['argv'] = argv
    registration['controller']['process']['argv'] = argv.copy()
    old_templates = phase['controller_templates']
    templates = copy.deepcopy(objects[old_templates['path']])
    templates['templates'][0]['argv_template_at_proposed_cap'][2:] = argv
    templates_pin = ref(Path(old_templates['path']).name, templates)
    phase['controller_templates'] = templates_pin
    registration['controller_templates'] = templates_pin
    for continuation in (phase['continuation'], registration['continuation']):
        continuation['control'] = control_pin
        continuation['binding'].update(schema='causes.corpus_continuation_binding.v2',
                                       full_selection=copy.deepcopy(SELECTION))
    changes = {old_control['path']: control_pin, old_templates['path']: templates_pin}
    registration['controller']['input_pins'] = [changes.get(row['path'], row)
                                               for row in registration['controller']['input_pins']]
    return policy, registration, objects


class MixedAllocationTests(unittest.TestCase):
    def test_legacy_prefix_blocks_keep_frozen_identity(self):
        # Captured from unchanged main at e948377 using the existing fixture.
        expected = ['e1d9f0e54b76856fed8985a10b601f0171b1abc57622a97c03bfbe3aafb3705f',
                    'deea42244e5525e54b0d904995d7cbaffe6375d514f3e9280d2f617c9e63d183']
        allocator = CorpusContinuation(**fixture())
        for index, checksum in enumerate(expected):
            block = allocator.block(index)
            self.assertEqual(digest(block), checksum)
            self.assertEqual(block['binding']['schema'], 'causes.corpus_continuation_binding.v1')
            self.assertNotIn('full_selection', block['binding'])
            self.assertFalse(full_set(block))
            allocator.accept(block)

    def test_mixed_mode_changes_only_mode_binding_and_continue_to_end(self):
        args = fixture()
        before = copy.deepcopy(args)
        prefix = CorpusContinuation(**args)
        mixed = CorpusContinuation(**args, full_selection=SELECTION)
        for index in range(3):
            left, right = prefix.block(index), mixed.block(index)
            self.assertEqual(right['schema'], left['schema'])
            self.assertEqual(right['seed_ledger'], left['seed_ledger'])
            self.assertEqual(right['reserved_seeds_before_sha256'], left['reserved_seeds_before_sha256'])
            self.assertEqual(right['reserved_seeds_after_sha256'], left['reserved_seeds_after_sha256'])
            binding = copy.deepcopy(right['binding'])
            self.assertEqual(binding.pop('full_selection'), SELECTION)
            binding['schema'] = 'causes.corpus_continuation_binding.v1'
            self.assertEqual(binding, left['binding'])
            for old, new in zip(left['jobs'], right['jobs']):
                normalized = copy.deepcopy(new)
                normalized['allocation']['continue_to_end'] = False
                self.assertEqual(normalized, old)
            prefix.accept(left)
            mixed.accept(right)
        self.assertEqual(args, before)

    def test_one_eligible_full_cell_per_task_rotates_over_all_cells(self):
        args = fixture()
        held_only = copy.deepcopy(args['catalog']['physical_cells'][-1])
        held_only['task_id'] = 'held-only-task'
        args['catalog']['physical_cells'].append(held_only)
        eligible = {key(cell) for cell in admissible_cells(args['catalog'])}
        allocator = CorpusContinuation(**args, full_selection=SELECTION)
        selections = []
        for index in range(6):
            block = allocator.block(index)
            selected = full_set(block)
            self.assertEqual({task for task, _ in selected}, {'one', 'two'})
            self.assertEqual(len(selected), 2)
            self.assertEqual({physical(job) for job in block['jobs']}, eligible)
            self.assertTrue(selected <= eligible)
            selections.append(selected)
            allocator.accept(block)
        self.assertEqual(set.union(*selections), eligible)
        for task, period in [('one', 2), ('two', 3)]:
            sequence = [next(stimulus for name, stimulus in selected if name == task)
                        for selected in selections]
            self.assertEqual(len(set(sequence[:period])), period)
            self.assertEqual(sequence[period:], sequence[:-period])

    def test_physical_selection_shared_across_models_despite_distinct_ids_and_seeds(self):
        args = fixture()
        other = {**args, 'model_key': 'tex9', 'task_host': 'other-task-host',
                 'provider_host': 'other-provider-host'}
        left = CorpusContinuation(**args, full_selection=SELECTION)
        right = CorpusContinuation(**other, full_selection=SELECTION)
        for index in range(4):
            a, b = left.block(index), right.block(index)
            self.assertEqual(full_set(a), full_set(b))
            self.assertFalse({j['allocation']['run_id'] for j in a['jobs']} &
                             {j['allocation']['run_id'] for j in b['jobs']})
            self.assertNotEqual(a['seed_ledger'], b['seed_ledger'])
            left.accept(a)
            right.accept(b)

    def test_reordered_input_cells_keep_selection_and_episode_order(self):
        args = fixture()
        shuffled = copy.deepcopy(args)
        for name in ('prototypes', 'original_jobs'):
            shuffled[name].reverse()
        shuffled['catalog']['physical_cells'].reverse()
        left = CorpusContinuation(**args, full_selection=SELECTION)
        right = CorpusContinuation(**shuffled, full_selection=SELECTION)
        for index in range(3):
            a, b = left.block(index), right.block(index)
            self.assertEqual(full_set(a), full_set(b))
            normalized = copy.deepcopy(b['jobs'])
            for left_job, right_job in zip(a['jobs'], normalized):
                self.assertEqual(left_job['allocation']['coverage_sha256'], a['binding']['catalog_sha256'])
                self.assertEqual(right_job['allocation']['coverage_sha256'], b['binding']['catalog_sha256'])
                self.assertNotEqual(a['binding']['catalog_sha256'], b['binding']['catalog_sha256'])
                right_job['allocation']['coverage_sha256'] = left_job['allocation']['coverage_sha256']
            self.assertEqual(a['jobs'], normalized)
            self.assertEqual(a['seed_ledger'], b['seed_ledger'])
            left.accept(a)
            right.accept(b)

    def test_restart_preserves_pending_block_and_rejects_mode_or_job_tampering(self):
        original = CorpusContinuation(**fixture(), full_selection=SELECTION)
        first = original.block(0)
        self.assertEqual(original.block(0), first)
        original.accept(first)
        second = original.block(1)
        restarted = CorpusContinuation(**fixture(), full_selection=SELECTION)
        restarted.accept(first)
        self.assertEqual(restarted.block(1), second)
        for mutate in (
            lambda b: b['binding'].pop('full_selection'),
            lambda b: b['binding']['full_selection'].update(method='other'),
            lambda b: b['jobs'][0]['allocation'].update(
                continue_to_end=not b['jobs'][0]['allocation']['continue_to_end']),
            lambda b: b.update(block_index=1.0),
        ):
            damaged = copy.deepcopy(second)
            mutate(damaged)
            with self.assertRaisesRegex(ValueError, 'block_or_ledger_mismatch'):
                restarted.accept(damaged)
            self.assertEqual(restarted.index, 1)
        restarted.accept(second)
        with self.assertRaisesRegex(ValueError, 'block_or_ledger_mismatch'):
            CorpusContinuation(**fixture()).accept(first)

    def test_full_mode_does_not_relax_frozen_prefix_only_prototypes(self):
        for section in ('prototypes', 'original_jobs'):
            args = fixture()
            args[section][0]['allocation']['continue_to_end'] = True
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, 'prefix_only'):
                CorpusContinuation(**args, full_selection=SELECTION)
        for invalid in ({}, {'method': 'other'}, {**SELECTION, 'additional': True}, True, []):
            with self.subTest(selection=invalid), self.assertRaisesRegex(ValueError, 'unsupported_full_selection'):
                CorpusContinuation(**fixture(), full_selection=invalid)

    def test_explicit_v2_control_required_and_full_mode_bound(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            control, _ = mixed_control(directory, args)
            allocator = CorpusContinuation.from_control(control, args['prototypes'], args['config'])
            self.assertEqual(allocator.binding['schema'], 'causes.corpus_continuation_binding.v2')
            self.assertEqual(allocator.binding['full_selection'], SELECTION)
            self.assertEqual(len(full_set(allocator.block(0))), 2)
            for schema, selection in [('causes.corpus_continuation_control.v1', None),
                                      ('causes.corpus_continuation_control.v1', SELECTION),
                                      ('causes.corpus_continuation_control.v2', None),
                                      ('causes.corpus_continuation_control.v2', {}),
                                      ('causes.corpus_continuation_control.v2', {**SELECTION, 'extra': 1}),
                                      ('causes.corpus_continuation_control.v3', SELECTION)]:
                changed = {**control, 'schema': schema, 'full_selection': selection}
                with self.subTest(schema=schema, selection=selection), self.assertRaises(ValueError):
                    CorpusContinuation.from_control(changed, args['prototypes'], args['config'])
            legacy = {k: v for k, v in control.items() if k != 'full_selection'}
            legacy['schema'] = 'causes.corpus_continuation_control.v1'
            self.assertFalse(full_set(CorpusContinuation.from_control(
                legacy, args['prototypes'], args['config']).block(0)))


class MixedDispatcherTests(unittest.TestCase):
    def test_full_producing_control_requires_positive_cap_and_empty_initial_queue(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, control = mixed_control(root, args)
            kwargs = dict(extension_prototypes=args['prototypes'], workers=2,
                          corpus_continuation=control['path'],
                          corpus_continuation_sha256=control['sha256'])
            with self.assertRaisesRegex(ValueError, 'full_continuation_requires_positive_full_limit'):
                Dispatcher([], args['config'], root / 'zero-cap', max_full=0, **kwargs)
            with self.assertRaisesRegex(ValueError, 'continuation_requires_empty_initial_queue'):
                Dispatcher([args['prototypes'][0]], args['config'], root / 'nonempty', max_full=1, **kwargs)
            dispatcher = Dispatcher([], args['config'], root / 'positive', max_full=1, **kwargs)
            self.assertEqual(dispatcher.max_full, 1)
            self.assertEqual(len(full_set(dispatcher.continuation.block(0))), 2)

    def test_two_synthetic_blocks_pause_and_restart_without_replaying_full_jobs(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, control = mixed_control(root, args)
            pause = root / 'PAUSE'
            seen = []
            def runner(spec, variant, config, allocation, output):
                seen.append(copy.deepcopy(allocation))
                result = finish(spec, variant, config, allocation, output)
                if len(seen) == 10:
                    pause.touch()
                return result
            kwargs = dict(workers=1, max_full=1, extension_prototypes=args['prototypes'],
                          runner=runner, pause_file=pause, poll_seconds=0.001,
                          corpus_continuation=control['path'],
                          corpus_continuation_sha256=control['sha256'])
            output = root / 'runs'
            result = Dispatcher([], args['config'], output, **kwargs).run()
            self.assertEqual(result['next_extension_block'], 2)
            self.assertEqual(len(seen), 10)
            self.assertEqual(len({a['run_id'] for a in seen}), 10)
            self.assertEqual(sum(a['continue_to_end'] for a in seen), 4)
            self.assertTrue(all(not a['fixed_study_inference'] and not a['reader_stages'] for a in seen))
            Dispatcher([], args['config'], output, **kwargs).run()
            self.assertEqual(len(seen), 10)
            blocks = sorted((output / '.dispatcher/blocks').glob('[0-9]*.json'))
            self.assertEqual(len(blocks), 2)
            blocks[-1].unlink()
            with self.assertRaises(ValueError):
                Dispatcher([], args['config'], output, **kwargs).run()


class MixedMonitorTests(unittest.TestCase):
    def test_monitor_reconstructs_exact_mixed_binding_and_preserves_history_scope(self):
        policy, registration, objects = mixed_monitor_fixture()
        reader = lambda ref: copy.deepcopy(objects[ref['path']])
        phase = monitor.validate_registration(registration, policy, reader)
        self.assertEqual(phase['mode'], 'continuous')
        self.assertEqual(phase['continuation']['binding']['schema'], 'causes.corpus_continuation_binding.v2')
        self.assertEqual(phase['continuation']['binding']['full_selection'], SELECTION)
        self.assertEqual(registration['donor_histories'], phase['donor_histories'])
        for mutate in (lambda b: b.pop('full_selection'),
                       lambda b: b.update(schema='causes.corpus_continuation_binding.v1'),
                       lambda b: b['full_selection'].update(method='other')):
            p, r = copy.deepcopy((policy, registration))
            mutate(r['continuation']['binding'])
            p['phases'][-1]['continuation'] = copy.deepcopy(r['continuation'])
            with self.assertRaisesRegex(ValueError, 'continuation_object_binding_changed'):
                monitor.validate_registration(r, p, reader)

    def test_monitor_refuses_zero_or_excess_full_cap(self):
        for cap in (0, 9):
            policy, registration, objects = mixed_monitor_fixture(max_full=cap)
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, 'full_continuation_requires_positive_full_limit'):
                monitor.validate_registration(registration, policy, lambda ref: objects[ref['path']])


if __name__ == '__main__':
    unittest.main()
