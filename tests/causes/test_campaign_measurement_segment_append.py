import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_measurement_reuse as reuse
from causes import campaign_measurement_segment_append as append


class SegmentAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0
        self.native_source = self.write('native-source', {'fixture': True})
        self.names = list(append.GROUPS['self'] + append.GROUPS['reader'])
        self.assignments = dict(epochs={'b': 'old', 't': 'new', 'x': 'new', 'y': 'new'},
            readout_names=self.names,
            primary_readouts=['self_evaluation_presence', 'reader_presence'],
            assignments=[dict(factor='contrast', members={
                'task': dict(control=['b'], treatment=['t', 'x', 'y'])})])
        self.assignment_ref = self.write('assignments', self.assignments)
        self.assignment_review = self.write('assignment-review', dict(
            accepted_fixture_assignments=True, assignments=self.assignment_ref, findings=[]))
        self.base_rows = [self.row('b', name) for name in self.names]
        self.old_rows = [self.row('t', self.names[0], missing=True, epoch=False),
                         self.row('t', self.names[1], epoch=False)]
        self.previous_segments = [self.segment(self.base_rows, 'custody', new=False),
                                  self.segment(self.old_rows[:1], 'slots', new=False),
                                  self.segment(self.old_rows[1:], 'output_slots', new=False)]
        self.new_rows = [self.row('t', name, missing=index == 1)
                         for index, name in enumerate(append.GROUPS['reader'])]
        self.additions = [self.segment(self.new_rows, 'slots', group_kind='reader'),
                          self.segment([self.row('x', name) for name in append.GROUPS['self']],
                                       'slots', group_kind='self', count='counts.occupied')]
        self.merge_source = append.pin(reuse.__file__)
        self.previous = self.make_previous(self.previous_segments,
                                           self.base_rows + self.old_rows)

    def write(self, name, value):
        self.serial += 1
        path = self.root / (str(self.serial) + '-' + name + '.json')
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
        return append.pin(path)

    def row(self, run, name, *, missing=False, epoch=True):
        result = dict(run_id=run, readout=name, status='missing' if missing else 'observed',
                      label=None if missing else 'yes', reason='native_failure' if missing else None,
                      evidence={'original': run + '/' + name})
        if epoch:
            result['epoch'] = self.assignments['epochs'].get(run, 'new')
        return result

    def segment(self, rows, kind, *, new=True, group_kind='reader', count='occupied',
                epoch_policy=None):
        payload = self.write('slots', rows)
        result = dict(source=self.native_source, assignments=self.assignment_ref)
        if kind == 'custody':
            result.update(outputs={'MEASUREMENT_CUSTODY': payload},
                          manifest=self.write('native-manifest', {'assignments': self.assignment_ref}),
                          physical_measurements=len(rows))
        elif kind == 'slots':
            result['slots'] = payload
        else:
            result['outputs'] = {'slots': payload}
        if new:
            runs = list(dict.fromkeys(row['run_id'] for row in rows))
            if group_kind == 'reader':
                result['membership'] = self.write('membership', {'terminal_run_ids': runs})
                membership = 'terminal_run_ids'
            else:
                result['selected_run_ids'] = runs
                membership = 'selected_run_ids'
            if count == 'occupied':
                result['occupied'] = len(rows)
            else:
                result['counts'] = {'occupied': len(rows)}
            group = dict(kind=group_kind, membership=membership, count=count)
        else:
            group = None
        result_ref = self.write('native-result', result)
        review = dict(result, accepted_fixture_segment=True, actual=result_ref, findings=[])
        review_ref = self.write('native-review', review)
        segment = dict(role='fixture', result=result_ref, review=review_ref, source=self.native_source,
                       payload=payload, acceptance_flag='accepted_fixture_segment',
                       physical_measurements=len(rows), epoch_policy='saved-policy')
        if kind == 'custody':
            segment['original_assignments'] = self.assignment_ref
        return dict(kind=kind, epoch_policy=epoch_policy or ('required' if new or kind == 'custody'
                                                            else 'optional_match'),
                    segment=segment, group=group)

    def make_previous(self, descriptors, rows, *, coverage_edit=None):
        counts = append.coverage(rows, self.assignments)
        if coverage_edit:
            coverage_edit(counts)
        coverage_ref = self.write('coverage', counts)
        manifest = dict(schema='fixture-manifest', assignments=self.assignment_ref,
            segments=[item['segment'] for item in descriptors], coverage=coverage_ref,
            physical_measurements=len(rows), all_primary_terminal=False,
            missing_preserved=True, final_holm_available=False)
        result = dict(source=self.native_source, assignments=self.assignment_ref,
            assignment_review=self.assignment_review, public_merge_source=self.merge_source,
            manifest=self.write('manifest', manifest), coverage=coverage_ref,
            terminal_slots=len(rows), pending_slots=counts['pending_slots'])
        ref = self.write('previous-result', result)
        peer = self.write('previous-review', dict(result, actual=ref,
            accepted_fixture_previous=True, findings=[]))
        return dict(result=ref, review=peer, acceptance_flag='accepted_fixture_previous')

    def bindings(self, **changes):
        value = dict(schema=append.SCHEMA, assignments=self.assignment_ref,
            assignment_review=self.assignment_review, assignment_acceptance_flag='accepted_fixture_assignments',
            public_merge_source=self.merge_source, previous=self.previous,
            previous_segments=self.previous_segments, additions=self.additions)
        value.update(changes)
        return self.write('bindings', value)

    def request(self, bindings):
        source = append.pin(append.__file__)
        source_review = self.write('source-review', dict(
            accepted_measurement_segment_append_source=True, source=source, findings=[]))
        binding_review = self.write('bindings-review', dict(
            accepted_measurement_segment_append_bindings=True, source=source,
            source_review=source_review, bindings=bindings, findings=[]))
        return self.write('request', dict(schema='causes.measurement_segment_append_request.v1',
            source=source, source_review=source_review, bindings=bindings, bindings_review=binding_review,
            root_authorized=True, maximum_invocations=1, automatic_retry=False,
            model_calls=0, host_calls=0, payload_bytes_copied=0, output=str(self.root / 'output')))

    def test_complete_append_preserves_payloads_and_optional_epochs(self):
        before = {item['segment']['payload']['path']: Path(item['segment']['payload']['path']).read_bytes()
                  for item in self.previous_segments + self.additions}
        ref = append.run(self.request(self.bindings()))
        result = append.checked(ref)
        self.assertEqual((result['original_slots_retained'], result['added_slots'],
                          result['terminal_slots'], result['pending_slots']), (8, 6, 14, 10))
        self.assertEqual(result['added_status_counts'], {'observed': 5, 'missing': 1})
        manifest = append.checked(result['manifest'])
        self.assertEqual(manifest['segments'][:3], [d['segment'] for d in self.previous_segments])
        self.assertEqual({path.name for path in (self.root / 'output').iterdir()},
                         {'INTENT.v1.json', 'COVERAGE.v1.json', 'MANIFEST.v1.json',
                          'MANIFEST_DERIVATION.v1.json', 'RESULT.v1.json'})
        for path, body in before.items():
            self.assertEqual(Path(path).read_bytes(), body)
        self.assertFalse(result['analysis_performed'])
        self.assertFalse(result['inference_released'])
        self.assertTrue(append.checked(result['manifest_derivation'])['whole_inverse_equal'])

    def test_next_batch_accepts_generic_predecessor(self):
        result_ref = append.run(self.request(self.bindings()))
        result = append.checked(result_ref)
        review = self.write('new-previous-review', dict(result, actual=result_ref,
            accepted_fixture_previous=True, findings=[]))
        descriptors = copy.deepcopy(self.previous_segments + self.additions)
        for item in descriptors:
            item['group'] = None
        addition = self.segment([self.row('y', name) for name in append.GROUPS['reader']], 'slots')
        derived = append.derive(self.bindings(previous=dict(result=result_ref, review=review,
            acceptance_flag='accepted_fixture_previous'), previous_segments=descriptors,
            additions=[addition]))
        self.assertEqual(derived[-1]['original_slots_retained'], 14)
        self.assertEqual(derived[-1]['added_slots'], 3)
        self.assertEqual(derived[-2]['terminal_slots'], 17)

    def test_missing_slots_are_occupied_and_partial_overlap_refuses(self):
        addition = self.segment([self.row('t', name) for name in append.GROUPS['self']],
                                'slots', group_kind='self')
        with self.assertRaisesRegex(ValueError, 'partial_measurement_group_overlap'):
            append.derive(self.bindings(additions=[addition]))

    def test_full_replay_refuses(self):
        addition = self.segment([self.row('b', name) for name in append.GROUPS['reader']], 'slots')
        with self.assertRaisesRegex(ValueError, 'occupied_measurement_group_cannot_be_replayed'):
            append.derive(self.bindings(additions=[addition]))

    def test_partial_instrument_group_refuses(self):
        addition = self.segment(self.new_rows[:2], 'slots')
        with self.assertRaisesRegex(ValueError, 'partial_or_extra_instrument_group'):
            append.derive(self.bindings(additions=[addition]))

    def test_duplicate_rows_and_duplicate_segments_refuse(self):
        addition = self.segment(self.new_rows + self.new_rows[:1], 'slots')
        with self.assertRaisesRegex(ValueError, 'duplicate_physical_measurement'):
            append.derive(self.bindings(additions=[addition]))
        with self.assertRaisesRegex(ValueError, 'duplicate_measurement_group'):
            append.derive(self.bindings(additions=[self.additions[0], self.additions[0]]))

    def test_unknown_run_or_instrument_refuses(self):
        for key, value in [('run_id', 'unknown'), ('readout', 'unknown')]:
            with self.subTest(key=key):
                rows = copy.deepcopy(self.new_rows)
                rows[0][key] = value
                with self.assertRaisesRegex(ValueError, 'measurement_outside_frozen_assignment'):
                    append.derive(self.bindings(additions=[self.segment(rows, 'slots')]))

    def test_changed_epoch_refuses(self):
        rows = copy.deepcopy(self.new_rows)
        rows[0]['epoch'] = 'different'
        for policy in ('required', 'optional_match'):
            with self.subTest(policy=policy):
                addition = self.segment(rows, 'slots', epoch_policy=policy)
                with self.assertRaisesRegex(ValueError, 'measurement_epoch_changed'):
                    append.derive(self.bindings(additions=[addition]))

    def test_required_epoch_cannot_be_omitted_or_base_relaxed(self):
        rows = copy.deepcopy(self.new_rows)
        del rows[0]['epoch']
        with self.assertRaisesRegex(ValueError, 'measurement_epoch_changed'):
            append.derive(self.bindings(additions=[self.segment(rows, 'slots')]))
        descriptors = copy.deepcopy(self.previous_segments)
        descriptors[0]['epoch_policy'] = 'optional_match'
        with self.assertRaisesRegex(ValueError, 'base_epoch_required'):
            append.derive(self.bindings(previous_segments=descriptors))

    def test_removed_or_relabelled_previous_segment_refuses(self):
        with self.assertRaisesRegex(ValueError, 'previous_segments_changed'):
            append.derive(self.bindings(previous_segments=self.previous_segments[:-1]))
        descriptors = copy.deepcopy(self.previous_segments)
        descriptors[1]['segment']['physical_measurements'] = True
        with self.assertRaisesRegex(ValueError, 'previous_segments_changed'):
            append.derive(self.bindings(previous_segments=descriptors))

    def test_previous_coverage_must_reconstruct_exactly(self):
        previous = self.make_previous(self.previous_segments, self.base_rows + self.old_rows,
                                     coverage_edit=lambda c: c.update(terminal_slots=9))
        with self.assertRaisesRegex(ValueError, 'previous_coverage_changed'):
            append.derive(self.bindings(previous=previous))

    def test_invalid_missing_disposition_refuses(self):
        rows = copy.deepcopy(self.new_rows)
        rows[1]['label'] = 'yes'
        with self.assertRaisesRegex(ValueError, 'explicit_missing_measurement_required'):
            append.derive(self.bindings(additions=[self.segment(rows, 'slots')]))

    def test_changed_payload_pin_refuses(self):
        payload = self.additions[0]['segment']['payload']
        Path(payload['path']).write_text('[]\n')
        with self.assertRaisesRegex(ValueError, 'file_pin_changed'):
            append.derive(self.bindings())

    def test_request_requires_authority_and_fresh_output(self):
        ref = self.request(self.bindings())
        request = append.checked(ref)
        request['root_authorized'] = False
        with self.assertRaisesRegex(ValueError, 'append_request_not_authorized'):
            append.run(self.write('unauthorized', request))
        append.run(ref)
        with self.assertRaisesRegex(ValueError, 'fresh_output_required'):
            append.run(ref)

    def test_authority_numbers_are_not_booleans_or_floats(self):
        request = append.checked(self.request(self.bindings()))
        for key, value in [('maximum_invocations', True), ('maximum_invocations', 1.0),
                           ('model_calls', False), ('host_calls', 0.0)]:
            with self.subTest(key=key, value=value):
                changed = dict(request, **{key: value})
                with self.assertRaisesRegex(ValueError, 'append_request_not_authorized'):
                    append.run(self.write('wrong-type', changed))


if __name__ == '__main__':
    unittest.main()
