import copy
import hashlib
import json
import unittest

from causes import campaign_measurement_reuse as reuse


class MeasurementReuseTests(unittest.TestCase):
    epochs = {'old': 'historical', 'new': 'fresh'}
    names = ['self_presence', 'reader_presence', 'reader_strong']

    def row(self, run='old', name='self_presence', status='observed', **extra):
        return dict(run_id=run, readout=name, status=status,
                    label='present' if status == 'observed' else None,
                    reason=None if status == 'observed' else 'technical_failure',
                    evidence={'original_request': 'unchanged'},
                    epoch=self.epochs[run], **extra)

    def merge(self, base, new):
        return reuse.merge_measurements(base, new, epochs=self.epochs,
                                        readout_names=self.names)

    def select(self, groups, base):
        return reuse.select_new_measurement_groups(
            groups, base, group_keys=lambda group: group['keys'],
            epochs=self.epochs, readout_names=self.names)

    def test_disjoint_instruments_share_run_and_keep_order(self):
        base = [self.row()]
        new = [self.row(name='reader_presence'), self.row('new')]
        self.assertEqual(self.merge(base, new), base + new)
        merged = self.merge(base, new)
        merged[0]['evidence']['original_request'] = 'changed'
        self.assertEqual(base[0]['evidence']['original_request'], 'unchanged')

    def test_existing_observed_or_missing_is_never_replaced(self):
        for status, reason in [('observed', None), ('missing', 'invalid_schema'),
                               ('missing', 'unresolved_current_reader_attempt'),
                               ('missing', 'unavailable_context')]:
            old = self.row(status=status)
            old['reason'] = reason
            with self.subTest(status=status, reason=reason):
                with self.assertRaisesRegex(ValueError, 'cannot_be_replaced'):
                    self.merge([old], [self.row()])

    def test_duplicate_new_rows_refuse_even_if_identical(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_physical_measurement'):
            self.merge([], [self.row(), self.row()])

    def test_invalid_terminal_status_or_reason_refuses(self):
        mutations = [dict(status='pending', label=None), dict(status='missing', reason=''),
                     dict(status='missing', label='present'), dict(status='observed', label=None)]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.merge([dict(self.row(), **changes)], [])

    def test_frozen_epoch_or_membership_cannot_change(self):
        for changes in [dict(epoch='fresh'), dict(run_id='unknown'), dict(readout='unknown')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.merge([dict(self.row(), **changes)], [])
        row = self.row()
        del row['epoch']
        with self.assertRaisesRegex(ValueError, 'epoch_changed'):
            self.merge([row], [])

    def test_delta_can_have_no_epoch_until_native_assembly(self):
        row = self.row('new')
        del row['epoch']
        self.assertEqual(self.merge([self.row()], [row]), [self.row(), row])

    def test_filter_happens_without_old_payload_enumeration(self):
        groups = [dict(keys=[('old', 'self_presence')], payload='must_not_read'),
                  dict(keys=[('old', 'reader_presence'), ('old', 'reader_strong')], payload='new')]
        selected = self.select(groups, {('old', 'self_presence')})
        calls = [group['payload'] for group in selected]
        self.assertEqual(calls, ['new'])
        self.assertIs(selected[0], groups[1])

    def test_partial_group_overlap_refuses(self):
        with self.assertRaisesRegex(ValueError, 'partial_measurement_group_overlap'):
            self.select([dict(keys=[('old', 'reader_presence'), ('old', 'reader_strong')])],
                        {('old', 'reader_presence')})

    def test_duplicate_and_unknown_groups_refuse_before_enumeration(self):
        groups = [dict(keys=[('old', 'self_presence')])]
        with self.assertRaisesRegex(ValueError, 'duplicate_measurement_group'):
            self.select(groups * 2, {('old', 'self_presence')})
        for key in [('unknown', 'self_presence'), ('old', 'unknown')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.select([dict(keys=[key])], set())

    def fixture(self):
        files = {}

        def put(name, value):
            raw = json.dumps(value, sort_keys=True).encode()
            ref = dict(path='/fixture/' + name, bytes=len(raw),
                       sha256=hashlib.sha256(raw).hexdigest())
            files[ref['path']] = raw
            return ref

        source = put('source', {})
        assignments = put('assignments', {})
        manifest = put('manifest', {'assignments': assignments, 'rows': []})
        custody = put('custody', [self.row()])
        result = dict(source=source, assignments=assignments, manifest=manifest,
                      outputs={'MEASUREMENT_CUSTODY': custody}, physical_measurements=1)
        result_ref = put('result', result)
        review = dict(accepted_measurements=True, actual=result_ref, source=source,
                      assignments=assignments, outputs=result['outputs'], manifest=manifest)
        review_ref = put('review', review)
        return files, put, source, assignments, result, result_ref, review, review_ref

    def load(self, fixture, *, record=None):
        files, _, source, assignments, _, result_ref, _, review_ref = fixture
        return reuse.load_accepted_measurements(
            result_ref, review_ref, assignments_reference=assignments,
            source_reference=source, acceptance_flag='accepted_measurements',
            record=record or (lambda ref, read: files[ref['path']]))

    def test_accepted_base_reads_only_three_named_tables(self):
        fixture = self.fixture()
        reads = []

        def record(ref, read):
            reads.append(ref['path'])
            return fixture[0][ref['path']]

        self.assertEqual(self.load(fixture, record=record), [self.row()])
        self.assertEqual(reads, ['/fixture/result', '/fixture/review', '/fixture/custody'])

    def test_base_bytes_changed_refuses(self):
        fixture = self.fixture()
        fixture[0]['/fixture/custody'] += b' '
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            self.load(fixture)

    def test_metadata_selection_never_reads_the_custody_table(self):
        files, _, source, assignments, _, result_ref, _, review_ref = self.fixture()
        reads = []

        def record(ref, read):
            self.assertNotEqual(ref['path'], '/fixture/custody')
            reads.append(ref['path'])
            return files[ref['path']]

        manifest = reuse.load_accepted_manifest(
            result_ref, review_ref, assignments_reference=assignments,
            source_reference=source, acceptance_flag='accepted_measurements', record=record)
        self.assertEqual(manifest['assignments'], assignments)
        self.assertEqual(reads, ['/fixture/result', '/fixture/review', '/fixture/manifest'])

    def test_re_pinned_wrong_review_or_assignment_refuses(self):
        for field in ['accepted_measurements', 'actual', 'source', 'assignments', 'outputs', 'manifest']:
            fixture = list(self.fixture())
            review = copy.deepcopy(fixture[6])
            review[field] = False if field == 'accepted_measurements' else {}
            fixture[7] = fixture[1]('review_changed', review)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'base_join'):
                self.load(fixture)

    def test_re_pinned_wrong_count_refuses(self):
        fixture = list(self.fixture())
        result = dict(fixture[4], physical_measurements=True)
        fixture[5] = fixture[1]('result_changed', result)
        fixture[7] = fixture[1]('review_changed', dict(fixture[6], actual=fixture[5]))
        with self.assertRaisesRegex(ValueError, 'measurement_count'):
            self.load(fixture)


class StreamingMeasurementSelectionTests(unittest.TestCase):
    epochs = {'old': 'historical', 'new': 'fresh'}
    names = ['self_presence', 'reader_presence', 'reader_strong']

    def selector(self, base=()):
        return reuse.MeasurementGroupSelector(set(base), epochs=self.epochs,
                                               readout_names=self.names)

    def test_stream_and_batch_keep_group_order_identity_and_missing_occupancy(self):
        rows = [dict(run_id='old', readout='self_presence', status='missing',
                     label=None, reason='unresolved_current_reader_attempt', evidence={}),
                dict(run_id='new', readout='reader_presence', status='observed',
                     label='present', evidence={})]
        base = reuse.measurement_keys(rows, epochs=self.epochs, readout_names=self.names)
        groups = [dict(keys=[('old', 'self_presence')]),
                  dict(keys=[('old', 'reader_presence'), ('old', 'reader_strong')]),
                  dict(keys=[('new', 'reader_presence')]),
                  dict(keys=[('new', 'self_presence')])]
        selector = self.selector(base)
        streamed = [group for group in groups if selector.admit(group['keys'])]
        batched = reuse.select_new_measurement_groups(
            groups, base, group_keys=lambda group: group['keys'],
            epochs=self.epochs, readout_names=self.names)
        self.assertEqual(streamed, [groups[1], groups[3]])
        self.assertEqual(streamed, batched)
        for actual, expected in zip(streamed, [groups[1], groups[3]]):
            self.assertIs(actual, expected)

    def test_duplicate_groups_refuse_across_calls_even_when_base_occupied(self):
        key = ('old', 'self_presence')
        for base in [set(), {key}]:
            with self.subTest(base=base):
                selector = self.selector(base)
                self.assertEqual(selector.admit([key]), not base)
                with self.assertRaisesRegex(ValueError, 'duplicate_measurement_group'):
                    selector.admit([key])
                with self.assertRaisesRegex(ValueError, 'duplicate_measurement_group'):
                    reuse.select_new_measurement_groups(
                        [[key], [key]], base, group_keys=lambda group: group,
                        epochs=self.epochs, readout_names=self.names)
        with self.assertRaisesRegex(ValueError, 'duplicate_measurement_group'):
            self.selector().admit([key, key])

    def test_partial_overlap_refuses_in_both_apis(self):
        keys = [('old', 'reader_presence'), ('old', 'reader_strong')]
        base = {keys[0]}
        with self.assertRaisesRegex(ValueError, 'partial_measurement_group_overlap'):
            self.selector(base).admit(keys)
        with self.assertRaisesRegex(ValueError, 'partial_measurement_group_overlap'):
            reuse.select_new_measurement_groups(
                [keys], base, group_keys=lambda group: group,
                epochs=self.epochs, readout_names=self.names)

    def test_caller_mutations_do_not_change_assignment_or_base_snapshot(self):
        epochs, names = dict(self.epochs), list(self.names)
        base = {('old', 'self_presence')}
        selector = reuse.MeasurementGroupSelector(base, epochs=epochs, readout_names=names)
        epochs.clear()
        epochs['foreign'] = 'fresh'
        names.clear()
        names.append('unknown')
        base.clear()
        base.add(('new', 'reader_presence'))
        self.assertFalse(selector.admit([('old', 'self_presence')]))
        self.assertTrue(selector.admit([('new', 'reader_presence')]))
        for key in [('foreign', 'self_presence'), ('new', 'unknown')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'outside_assignment'):
                selector.admit([key])

    def test_invalid_frozen_metadata_refuses_before_any_group(self):
        cases = [({}, self.names), (None, self.names), ({'old': ''}, self.names),
                 ({1: 'historical'}, self.names), (self.epochs, []),
                 (self.epochs, 'self_presence'), (self.epochs, ['']),
                 (self.epochs, ['reader_presence', 'reader_presence'])]
        for epochs, names in cases:
            with self.subTest(epochs=epochs, names=names):
                with self.assertRaisesRegex(ValueError, 'frozen_measurement_'):
                    reuse.MeasurementGroupSelector(set(), epochs=epochs, readout_names=names)
                with self.assertRaisesRegex(ValueError, 'frozen_measurement_'):
                    reuse.select_new_measurement_groups(
                        [], set(), group_keys=lambda group: group,
                        epochs=epochs, readout_names=names)

    def test_invalid_base_and_declared_keys_refuse(self):
        for key in [('foreign', 'self_presence'), ('old', 'unknown'), ('old',),
                    ('old', 'self_presence', 'extra'), (1, 'self_presence')]:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'base_measurement_keys_outside_assignment'):
                    self.selector({key})
                with self.assertRaisesRegex(ValueError, 'measurement_group_outside_assignment'):
                    self.selector().admit([key])
        for group in [[], (), None, {('old', 'self_presence')}]:
            with self.subTest(group=group), self.assertRaisesRegex(
                    ValueError, 'explicit_measurement_group_keys_required'):
                self.selector().admit(group)
        with self.assertRaisesRegex(ValueError, 'measurement_group_outside_assignment'):
            self.selector().admit([['old', 'self_presence']])

    def test_base_validation_work_does_not_grow_with_stream_length(self):
        class CountedRun(str):
            hashes = 0

            def __hash__(self):
                CountedRun.hashes += 1
                return super().__hash__()

        epochs = {f'old{i}': 'historical' for i in range(1000)}
        epochs.update({f'new{i}': 'fresh' for i in range(100)})
        base = {(CountedRun(f'old{i}'), 'self_presence') for i in range(1000)}
        CountedRun.hashes = 0
        selector = reuse.MeasurementGroupSelector(base, epochs=epochs,
                                                  readout_names=self.names)
        construction_hashes = CountedRun.hashes
        self.assertGreaterEqual(construction_hashes, len(base))
        for i in range(100):
            self.assertTrue(selector.admit([(f'new{i}', 'self_presence')]))
        self.assertEqual(CountedRun.hashes, construction_hashes)


if __name__ == '__main__':
    unittest.main()
