import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest

from causes import campaign_original_self_batch as batch


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.ids = ['a', 'b', 'c', 'd']

    def test_fixed_order_not_copy_completion_order(self):
        result = batch.select_batch(self.ids, {'d', 'a', 'c'}, {'c', 'a', 'd'}, set(), batch_size=2)
        self.assertEqual(result['selected_run_ids'], ['a', 'c'])
        self.assertEqual(result['pending_copy_ids'], ['b'])
        self.assertEqual(result['ready_unselected_ids'], ['d'])

    def test_less_than_batch_size_stays_pending_without_final(self):
        result = batch.select_batch(self.ids, {'a'}, {'a'}, set(), batch_size=2)
        self.assertEqual(result['selected_run_ids'], [])
        self.assertEqual(result['ready_unselected_ids'], ['a'])

    def test_original_missing_slots_are_occupied_equally(self):
        occupied = {('a', name) for name in batch.READOUTS}
        result = batch.select_batch(self.ids, set(self.ids), set(self.ids), occupied, batch_size=2)
        self.assertEqual(result['selected_run_ids'], ['b', 'c'])
        self.assertEqual(result['already_occupied'], 1)

    def test_partial_original_group_refuses(self):
        with self.assertRaisesRegex(ValueError, 'partial_original_self_group'):
            batch.select_batch(self.ids, set(self.ids), set(self.ids), {('a', batch.READOUTS[0])})

    def test_initial_membership_is_all_or_pending(self):
        self.assertEqual(batch.select_batch(self.ids, {'a', 'c'}, {'a'}, set(),
                         initial_ids=['c', 'a'])['selected_run_ids'], [])
        self.assertEqual(batch.select_batch(self.ids, {'a', 'c'}, {'c', 'a'}, set(),
                         initial_ids=['c', 'a'])['selected_run_ids'], ['a', 'c'])

    def test_final_cannot_convert_incomplete_copy_into_missing(self):
        with self.assertRaisesRegex(ValueError, 'final_copy_pair_incomplete'):
            batch.select_batch(self.ids, set(self.ids), {'a'}, set(), final=True)
        self.assertEqual(batch.select_batch(self.ids, set(self.ids), set(self.ids), set(),
                         final=True)['selected_run_ids'], self.ids)

    def test_foreign_duplicate_and_boolean_batch_size_refuse(self):
        for ids, self_ids, size in [(self.ids + ['a'], {'a'}, 2), (self.ids, {'other'}, 2),
                                   (self.ids, {'a'}, True)]:
            with self.subTest(ids=ids, size=size), self.assertRaises(ValueError):
                batch.select_batch(ids, self_ids, {'a'}, set(), batch_size=size)


class BindingTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        documents = {}

        def save(name, value):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = batch.encoded(value) + b'\n'
            path.write_bytes(data)
            ref = dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            documents[str(path)] = value
            return ref

        summary = save('summary.json', dict(run_id='r', early_status='missing', probes=[],
                          sandbox_closure=dict(state='closed', overlay_mounted=False)))
        config = save('config.json', dict(questions=[]))
        job = dict(allocation=dict(run_id='r', continue_to_end=False, instruments=list(batch.SELF)))
        group = dict(config=config, row=dict(run_id='r', instruments=list(batch.SELF),
                                            original_refs={'summary.json': summary}))

        class Record:
            def __call__(self, ref, read=False):
                data = Path(ref['path']).read_bytes()
                if len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
                    raise ValueError('changed_pin')
                return data if read else ref

            def checked_json(self, ref):
                return json.loads(self(ref, read=True))

        record = Record()
        calls = []
        evidence = object()

        def profile(row, actual_record, actual_evidence):
            self.assertIs(actual_record, record)
            self.assertIs(actual_evidence, evidence)
            calls.append('profile')
            return evidence

        def bind(row, **kwargs):
            self.assertIs(kwargs['evidence_module'], evidence)
            self.assertEqual(kwargs['job'], job)
            calls.append('native_bind')
            return [dict(run_id='r', readout=name, status='missing', label=None,
                         reason='context_not_captured', evidence={'original_attempt': True})
                    for name in batch.READOUTS]

        inputs = dict(reader=types.SimpleNamespace(original_profile=profile),
                      api=types.SimpleNamespace(bind_original_self=bind), runner=object(),
                      evidence=evidence, labels=object(), materializer=object(), record=record)
        assignments = dict(epochs={'r': 'prospective'}, readout_names=list(batch.READOUTS))
        return [group], {'r': job}, assignments, inputs, calls, save

    def test_native_terminal_missing_emits_all_three_without_reader_results(self):
        with tempfile.TemporaryDirectory() as directory:
            groups, jobs, assignments, inputs, calls, save = self.fixture(directory)
            result = batch.bind_batch(groups, jobs, assignments, **inputs)
            self.assertEqual(calls, ['profile', 'native_bind'])
            self.assertEqual(result['counts'], dict(occupied=3, observed=0, missing=3))
            self.assertEqual(result['selected_run_ids'], ['r'])
            self.assertTrue(all(row['epoch'] == 'prospective' and row['label'] is None
                                for row in result['slots']))

    def test_unclosed_summary_is_not_final_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            groups, jobs, assignments, inputs, calls, save = self.fixture(directory)
            groups[0]['row']['original_refs']['summary.json'] = save('not_closed.json',
                dict(run_id='r', sandbox_closure=dict(state='running', overlay_mounted=True)))
            with self.assertRaisesRegex(ValueError, 'stable_terminal_summary_required'):
                batch.bind_batch(groups, jobs, assignments, **inputs)
            self.assertEqual(calls, [])

    def test_changed_summary_is_not_silently_classified_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            groups, jobs, assignments, inputs, calls, save = self.fixture(directory)
            Path(groups[0]['row']['original_refs']['summary.json']['path']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed_pin'):
                batch.bind_batch(groups, jobs, assignments, **inputs)

    def test_native_partial_group_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            groups, jobs, assignments, inputs, calls, save = self.fixture(directory)
            inputs['api'] = types.SimpleNamespace(bind_original_self=lambda *a, **k: [])
            with self.assertRaisesRegex(ValueError, 'native_three_slot_group'):
                batch.bind_batch(groups, jobs, assignments, **inputs)

    def test_assignment_mask_drift_refuses_before_native_call(self):
        with tempfile.TemporaryDirectory() as directory:
            groups, jobs, assignments, inputs, calls, save = self.fixture(directory)
            jobs['r']['allocation']['instruments'] = list(batch.SELF[:1])
            with self.assertRaisesRegex(ValueError, 'three_original_self_slots_required'):
                batch.bind_batch(groups, jobs, assignments, **inputs)
            self.assertEqual(calls, [])


class SnapshotMetadataTests(unittest.TestCase):
    def setUp(self):
        self.docs = {}
        self.refs = {}
        self.bodies = {}
        self.selection = self.save('/selection.json', dict(rows=[dict(relative_run='producer/r', run_id='r')],
                                                         selected_runs=['producer/r']))
        self.config = self.save('/configuration.json', dict(host='producer', remote_base='/remote',
            roots=['producer'], destination='/saved', free_space_floor_bytes=80*1024**3, limit=8,
            selected_runs=['producer/r'], reader_inputs_only=False, evidence_profile='early_self_probes'))
        names = batch.snapshot.EARLY_SELF_PROBE_FILES
        files = [dict(path=name, bytes=2, sha256='0'*64) for name in names[:5]]
        omitted = [dict(path=name, reason='named_self_probe_evidence_absent') for name in names[5:]]
        manifest = self.save('/saved/self_probe_attempts/cycle/MANIFEST.json',
            dict(schema='caus.selected_self_probe_manifest.v1', host='producer', base='/remote',
                 runs=[dict(run='producer/r', files=files, omitted=omitted, summary_sha256='0'*64, bytes=10)]))
        self.receipt = dict(run='producer/r', path='/saved/self_probe_attempts/cycle/data/producer/r',
            manifest=manifest['path'], manifest_sha256=manifest['sha256'], files=5, bytes=10,
            summary_sha256='0'*64, schema='caus.self_probe_receipt.v1', custody_kind='actor_self_probe_evidence')
        self.save('/saved/self_probe_attempts/cycle/VERIFIED.json',
                  dict(schema='caus.self_probe_verified.v1', runs=[self.receipt]))
        data = batch.encoded(self.receipt) + b'\n'
        self.index = self.save_raw('/saved_index.jsonl', data)
        self.cut = self.save('/cut.json', dict(selection=self.selection, configuration=self.config,
            destination='/saved', accepted=1, index=dict(saved=self.index, bytes=len(data),
            sha256=self.index['sha256'], original_path='/saved/self_probes.accepted.jsonl')))
        self.peer = self.save('/peer.json', dict(accepted_requested_actor_self_probe_input_cut=True,
            findings=[], actual=self.cut, selection=self.selection, configuration=self.config,
            index=self.index, accepted=1))

    def save_raw(self, name, body):
        ref = dict(path=name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        self.refs[name] = ref
        self.bodies[name] = body
        return ref

    def save(self, name, value):
        self.docs[name] = value
        return self.save_raw(name, batch.encoded(value))

    def project(self):
        return batch.accepted_snapshots(self.cut, self.peer, self.selection, self.config, kind='self',
            read=lambda r: copy.deepcopy(self.docs[r['path']]), raw=lambda r: self.bodies[r['path']],
            pin=lambda p: self.refs[str(p)])

    def test_native_named_absence_is_retained_without_inventing_a_probe(self):
        rows = self.project()
        self.assertEqual(set(rows), {'r'})
        self.assertEqual(set(rows['r']['files']), set(batch.snapshot.EARLY_SELF_PROBE_FILES[:5]))

    def test_peer_requires_direct_saved_index_pin(self):
        self.docs['/peer.json']['index'] = self.docs['/cut.json']['index']
        with self.assertRaisesRegex(ValueError, 'accepted_copy_cut_join'):
            self.project()

    def test_reader_profile_cannot_be_presented_as_self(self):
        self.docs['/configuration.json']['evidence_profile'] = None
        with self.assertRaisesRegex(ValueError, 'copy_profile_changed'):
            self.project()

    def test_missing_probe_file_without_named_omission_refuses(self):
        self.docs[self.receipt['manifest']]['runs'][0]['omitted'].pop()
        with self.assertRaisesRegex(ValueError, 'reader_input_manifest_scope_mismatch'):
            self.project()

    def test_changed_index_bytes_refuse_before_manifest_reads(self):
        self.bodies[self.index['path']] += b'\n'
        with self.assertRaisesRegex(ValueError, 'copy_index_changed'):
            self.project()


if __name__ == '__main__':
    unittest.main()
