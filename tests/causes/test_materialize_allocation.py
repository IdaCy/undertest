"""Temporary synthetic inputs exercise the real allocator and native guards."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_handoff as h
from causes import campaign_materialize_allocation as materializer
from causes import campaign_corpus_succession as succession
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_corpus_continuation import fixture
from tests.causes import test_native_corpus_succession as native_fixture


class MaterializerTests(unittest.TestCase):
    closure = native_fixture.ClosedIssuedTests.closure
    save = native_fixture.ClosedIssuedTests.save

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0

    def record(self, value):
        self.serial += 1
        return self.save(f'record-{self.serial}.json', value)

    def build(self, choice='qualification'):
        data = fixture()
        prefix = choice == 'prefix_fallback'
        control = dict(
            schema='causes.corpus_continuation_control.v1' if prefix
            else 'causes.corpus_continuation_control.v2',
            **{key: data[key] for key in
               ('family', 'model_key', 'task_host', 'provider_host')},
            repetition_start=1)
        for key in ('catalog', 'prototypes', 'original_jobs', 'config'):
            control[key] = self.record(data[key])
        reservation = self.record(data['reservation'])
        control['reservation'] = reservation
        if not prefix:
            control['full_selection'] = {'method': 'one_cell_per_task_rotating_v1'}
        allocator = CorpusContinuation.from_control(control, data['prototypes'], data['config'])
        block_jobs = allocator.block(0)['jobs']
        selected = list(reversed(block_jobs)) if choice == 'qualification' else []
        roster = self.record(dict(family=data['family'], rows=[
            dict(run_id=j['allocation']['run_id'],
                 continue_to_end=j['allocation']['continue_to_end']) for j in selected
        ])) if selected else None
        control['reservation'] = None
        template = dict(
            control=control, target_mode='finite_qualification' if selected else 'continuous_corpus',
            workload='prefix_only' if prefix else 'mixed', workers=8 if prefix else 4,
            max_full=0 if prefix else 1, output_root=str(self.root / 'new-runs'),
            selected_run_ids=[j['allocation']['run_id'] for j in selected] if selected else None)
        templates = self.record({'templates': {choice: template}})
        preparation = self.record({'source': h.pin(materializer.__file__), 'templates': templates})
        preparation_review = self.record({
            'accepted_native_allocation_materializer': True, 'preparation': preparation})
        predecessor, _ = self.closure()
        closed = h.checked(predecessor['closure']['evidence'])
        reservation_review = self.record(dict(
            accepted_native_allocation_reservation=True, reservation=reservation,
            predecessor_closure=predecessor['closure'], final_journal=closed['journal'],
            all_persisted_assignment_ids_and_numeric_seeds_included=True,
            no_unknown_or_duplicate_assignments=True))
        inputs = dict(templates=templates, preparation_review=preparation_review, choice=choice,
                      reservation=reservation, reservation_review=reservation_review,
                      closed_predecessor=predecessor, output_directory=str(self.root / 'allocation'),
                      roster=roster)
        self.bind(inputs)
        return inputs, selected, data

    def bind(self, inputs):
        inputs['binding'] = self.record({key: inputs[key] for key in (
            'templates', 'choice', 'reservation', 'reservation_review',
            'closed_predecessor', 'output_directory', 'roster')})
        inputs['delegated_adoption'] = self.record(dict(
            root_authorized=True, preparation_review=inputs['preparation_review'],
            choice=inputs['choice'], inputs=inputs['binding']))

    def test_finite_roster_preserves_order_jobs_modes_and_seed_reservation(self):
        inputs, expected_jobs, data = self.build()
        original = copy.deepcopy(inputs)
        original_templates = h.checked(inputs['templates'])
        result = h.checked(materializer.materialize(inputs))
        allocation = h.checked(result['allocation'])
        jobs, config, extensions, _ = succession.native_allocation(allocation)
        self.assertEqual(jobs, expected_jobs)
        self.assertEqual(config, data['config'])
        self.assertEqual(extensions, [])
        self.assertEqual((result['initial_jobs'], result['full_jobs']), (5, 2))
        self.assertEqual(inputs, original)
        self.assertEqual(h.checked(inputs['templates']), original_templates)
        self.assertIsNone(original_templates['templates']['qualification']['control']['reservation'])
        self.assertEqual(h.checked(allocation['allocation_control'])['reservation'], inputs['reservation'])
        reserved = set(data['reservation']['allocation_seeds'])
        for job in jobs:
            self.assertTrue(reserved.isdisjoint(job['allocation'][key] for key in
                ('seed', 'block_seed', 'task_seed', 'model_seed')))
        metadata = h.checked(result['assignment_metadata'])
        self.assertEqual([row['run_id'] for row in metadata['rows']],
                         [j['allocation']['run_id'] for j in jobs])
        self.assertEqual([row['job_sha256'] for row in metadata['rows']],
                         [succession.digest(j) for j in jobs])
        self.assertFalse(result['runtime_authorized'])

    def test_continuous_prefix_and_mixed_modes_keep_empty_initial_queues(self):
        for choice in ('prefix_fallback', 'mixed_successor'):
            with self.subTest(choice=choice):
                inputs, _, data = self.build(choice)
                inputs['output_directory'] += '-' + choice
                self.bind(inputs)
                result = h.checked(materializer.materialize(inputs))
                allocation = h.checked(result['allocation'])
                jobs, config, extensions, allocator = succession.native_allocation(allocation)
                self.assertEqual(jobs, [])
                self.assertEqual(config, data['config'])
                self.assertEqual(extensions, data['prototypes'])
                self.assertEqual(allocation['control'], allocation['allocation_control'])
                self.assertEqual(result['initial_jobs'], 0)
                modes = [j['allocation']['continue_to_end'] for j in allocator.block(0)['jobs']]
                self.assertEqual(any(modes), choice == 'mixed_successor')

    def test_explicit_reader_resolves_retained_closure_without_original_aliases(self):
        inputs, expected_jobs, _ = self.build()
        pair = inputs['closed_predecessor']['closure']
        closed = h.checked(pair['evidence'])
        references = [pair['evidence'], pair['review'], closed['never_claimed'], closed['pause_ownership']]
        aliases = {}
        for index, ref in enumerate(references):
            original = Path(ref['path'])
            local = self.root / f'custody-{index}.json'
            original.rename(local)
            aliases[json.dumps(ref, sort_keys=True)] = h.pin(local)
        reads = []

        def retained(ref):
            reads.append(ref)
            return h.checked(aliases[json.dumps(ref, sort_keys=True)])

        with self.assertRaises(FileNotFoundError):
            materializer.materialize(inputs)
        self.assertFalse(Path(inputs['output_directory']).exists())
        result = h.checked(materializer.materialize(inputs, closure_reader=retained))
        self.assertEqual(h.checked(h.checked(result['allocation'])['jobs']), expected_jobs)
        self.assertEqual(reads, references)
        self.assertTrue(all(not Path(ref['path']).exists() for ref in references))

    def test_unaccepted_reservation_or_wrong_final_journal_refuses_before_output(self):
        inputs, _, _ = self.build()
        original = h.checked(inputs['reservation_review'])
        for field, value in (
            ('accepted_native_allocation_reservation', False),
            ('final_journal', inputs['reservation']),
            ('all_persisted_assignment_ids_and_numeric_seeds_included', False),
            ('no_unknown_or_duplicate_assignments', False),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                inputs['reservation_review'] = self.record(changed)
                self.bind(inputs)
                with self.assertRaisesRegex(ValueError, 'reservation_not_bound'):
                    materializer.materialize(inputs)
                self.assertFalse(Path(inputs['output_directory']).exists())

    def test_binding_and_roster_mode_drift_refuse_before_output(self):
        inputs, _, _ = self.build()
        changed = copy.deepcopy(inputs)
        changed['output_directory'] += '-changed'
        with self.assertRaisesRegex(ValueError, 'allocation_binding_changed'):
            materializer.materialize(changed)
        roster = h.checked(inputs['roster'])
        roster['rows'][0]['continue_to_end'] = not roster['rows'][0]['continue_to_end']
        inputs['roster'] = self.record(roster)
        self.bind(inputs)
        with self.assertRaisesRegex(ValueError, 'physical_roster_mode_changed'):
            materializer.materialize(inputs)
        self.assertFalse(Path(inputs['output_directory']).exists())

    def test_wrong_materializer_source_peer_and_spent_output_are_rejected(self):
        inputs, _, _ = self.build()
        original_peer = inputs['preparation_review']
        preparation = h.checked(h.checked(original_peer)['preparation'])
        preparation['source'] = inputs['templates']
        inputs['preparation_review'] = self.record(dict(
            accepted_native_allocation_materializer=True, preparation=self.record(preparation)))
        with self.assertRaisesRegex(ValueError, 'unaccepted_allocation_materializer'):
            materializer.materialize(inputs)
        inputs['preparation_review'] = original_peer
        result = materializer.materialize(inputs)
        before = h.checked_bytes(result)
        with self.assertRaises(FileExistsError):
            materializer.materialize(inputs)
        self.assertEqual(h.checked_bytes(result), before)


if __name__ == '__main__':
    unittest.main()
