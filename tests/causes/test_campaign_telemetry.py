import base64
import copy
import hashlib
import re
import unittest
from unittest.mock import patch

from causes import campaign_telemetry as t


def captured(raw):
    return {'base64': base64.b64encode(raw).decode(), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def metrics(types, values):
    raw = ''.join('# TYPE ' + name + ' ' + kind + '\n' for name, kind in types)
    rows = [{'name': name, 'labels': labels, 'value': value} for name, labels, value in values]
    raw += ''.join(name + labels + ' ' + str(value) + '\n' for name, labels, value in values)
    return {'body': captured(raw.encode()), 'values': rows}


def label_pattern(reason):
    return r'(?:^|[,{])\s*(?:finish|finished_reason)="' + reason + r'"\s*(?=[,}])'


def policy():
    return {'name_pattern': re.compile('preempt|swap|error|failure', re.I),
            'histogram_families': ('engine:request_preemptions',),
            'required_names': ('engine:preemptions_total', 'engine:request_preemptions_sum'),
            'required_label_series': tuple(('engine:requests_total', label_pattern(reason))
                                           for reason in ('error', 'abort'))}


def fixture():
    return metrics([
        ('engine:preemptions_total', 'counter'),
        ('engine:preemptions_created', 'gauge'),
        ('engine:request_preemptions', 'histogram'),
        ('engine:request_preemptions_created', 'gauge'),
        ('engine:requests_total', 'counter'),
        ('engine:requests_created', 'gauge'),
        ('engine:tokens_total', 'counter'),
    ], [
        ('engine:preemptions_total', '{}', 0),
        ('engine:preemptions_created', '{}', 1234567890.25),
        ('engine:request_preemptions_sum', '{}', 0),
        ('engine:request_preemptions_count', '{}', 7),
        ('engine:request_preemptions_bucket', '{le="+Inf"}', 7),
        ('engine:request_preemptions_created', '{}', 1234567890.25),
        ('engine:requests_total', '{finished_reason="error"}', 0),
        ('engine:requests_total', '{finish="abort"}', 0),
        ('engine:requests_total', '{finish="stop"}', 5),
        ('engine:requests_total', '{finish="length"}', 2),
        ('engine:requests_created', '{}', 1234567890.25),
        ('engine:tokens_total', '{}', 1000),
    ])


def journal_append(previous=None, cursor=None, delta=b'{"event":"started"}\n', **identity):
    prefix = (previous or b'') + delta
    after = {'device': 1, 'inode': 2, 'bytes': len(prefix),
             'sha256': hashlib.sha256(prefix).hexdigest(), **identity}
    return {'schema': 'causes.verified_journal_append.v1', 'cursor_before': cursor,
            'cursor_after': after, 'offset': len(previous or b''), **captured(delta)}


class HealthSeriesTests(unittest.TestCase):
    def test_epochs_are_not_counts_and_histogram_population_is_excluded(self):
        selected = t.select_health_series(fixture(), **policy())
        self.assertEqual(selected, {
            ('engine:preemptions_total', '{}'): 0,
            ('engine:preemptions_created', '{}'): 1234567890.25,
            ('engine:request_preemptions_sum', '{}'): 0,
            ('engine:request_preemptions_created', '{}'): 1234567890.25,
            ('engine:requests_total', '{finished_reason="error"}'): 0,
            ('engine:requests_total', '{finish="abort"}'): 0,
            ('engine:requests_created', '{}'): 1234567890.25,
        })

    def test_population_and_success_growth_do_not_change_selected_series(self):
        before = fixture()
        after = copy.deepcopy(before)
        for row in after['values']:
            if row['name'].endswith(('_bucket', '_count')) or row['labels'] in (
                    '{finish="stop"}', '{finish="length"}'):
                row['value'] += 100
        self.assertEqual(t.select_health_series(before, **policy()),
                         t.select_health_series(after, **policy()))

    def test_faults_and_epoch_changes_remain_visible(self):
        original = fixture()
        selected = t.select_health_series(original, **policy())
        for index in (0, 1, 2, 5, 6, 7, 10):
            with self.subTest(index=index):
                changed = copy.deepcopy(original)
                changed['values'][index]['value'] += 1
                result = t.select_health_series(changed, **policy())
                key = changed['values'][index]['name'], changed['values'][index]['labels']
                self.assertEqual(result[key], selected[key] + 1)
                self.assertEqual(result.keys(), selected.keys())

    def test_label_matching_requires_exact_key_and_value(self):
        for labels in ('{finished_reason="not_error"}', '{finish="error_more"}',
                       '{unfinished_reason="error"}', '{note="error"}', '{}'):
            with self.subTest(labels=labels):
                value = fixture()
                value['values'][6]['labels'] = labels
                with self.assertRaisesRegex(ValueError, 'health_label_series_coverage_absent'):
                    t.select_health_series(value, **policy())
        value = fixture()
        value['values'][6]['labels'] = '{model="fixture", finished_reason="error", other="x"}'
        self.assertIn(('engine:requests_total', value['values'][6]['labels']),
                      t.select_health_series(value, **policy()))

    def test_missing_required_metric_or_failure_reason_refuses(self):
        for index in (0, 2, 6, 7):
            with self.subTest(index=index):
                value = fixture()
                del value['values'][index]
                with self.assertRaisesRegex(ValueError, 'coverage_absent'):
                    t.select_health_series(value, **policy())

    def test_metric_policy_is_supplied_by_caller(self):
        value = metrics([('service:dropped_total', 'counter'), ('service:requests_total', 'counter')],
                        [('service:dropped_total', '', 3), ('service:requests_total', '{status="bad"}', 2)])
        result = t.select_health_series(value, name_pattern='dropped',
                                        required_names=('service:dropped_total',),
                                        required_label_series=(('service:requests_total', r'status="bad"'),))
        self.assertEqual(result, {('service:dropped_total', ''): 3,
                                  ('service:requests_total', '{status="bad"}'): 2})

    def test_unknown_relevant_types_and_histogram_suffixes_refuse(self):
        for types, name in (([], 'engine:swap_total'),
                            ([('engine:swap_total', 'gauge')], 'engine:swap_total'),
                            ([('engine:other_errors', 'histogram')], 'engine:other_errors_sum'),
                            ([('engine:request_preemptions', 'histogram')],
                             'engine:request_preemptions_unknown')):
            with self.subTest(name=name):
                value = metrics(types, [(name, '', 0)])
                with self.assertRaisesRegex(ValueError, 'unrecognized_health'):
                    t.select_health_series(value, name_pattern='preempt|swap|error',
                                           histogram_families=('engine:request_preemptions',))

    def test_histogram_type_is_required_even_for_excluded_population(self):
        for suffix in ('_sum', '_count', '_bucket'):
            with self.subTest(suffix=suffix):
                value = metrics([('engine:preemptions', 'gauge')], [('engine:preemptions' + suffix, '', 0)])
                with self.assertRaisesRegex(ValueError, 'health_histogram_type'):
                    t.select_health_series(value, name_pattern='preempt',
                                           histogram_families=('engine:preemptions',))

    def test_nested_histogram_families_are_order_independent(self):
        value = metrics([('engine:requests', 'histogram'), ('engine:requests_errors', 'histogram')],
                        [('engine:requests_errors_sum', '', 0),
                         ('engine:requests_errors_count', '', 7),
                         ('engine:requests_errors_bucket', '{le="+Inf"}', 7)])
        for families in (('engine:requests', 'engine:requests_errors'),
                         ('engine:requests_errors', 'engine:requests')):
            with self.subTest(families=families):
                self.assertEqual(t.select_health_series(value, name_pattern='error',
                                                         histogram_families=families),
                                 {('engine:requests_errors_sum', ''): 0})
                changed = copy.deepcopy(value)
                changed['values'][0]['name'] = 'engine:requests_errors_unknown'
                with self.assertRaisesRegex(ValueError, 'unrecognized_health_histogram_series'):
                    t.select_health_series(changed, name_pattern='error', histogram_families=families)

    def test_epoch_requires_gauge_and_counter_or_histogram_family(self):
        for types in ([('engine:errors_created', 'gauge')],
                      [('engine:errors_created', 'counter'), ('engine:errors_total', 'counter')],
                      [('engine:errors_created', 'gauge'), ('engine:errors', 'summary')]):
            with self.subTest(types=types):
                with self.assertRaisesRegex(ValueError, 'unrecognized_health_metric_epoch'):
                    t.select_health_series(metrics(types, [('engine:errors_created', '', 1)]),
                                           name_pattern='error')

    def test_duplicate_type_or_malformed_type_metadata_refuses(self):
        for raw in (b'# TYPE errors_total counter\n# TYPE errors_total counter\n',
                    b'# TYPE errors_total\n', b'# TYPE errors_total counter extra\n'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, 'metric_type_metadata'):
                    t.select_health_series({'body': captured(raw), 'values': []}, name_pattern='error')

    def test_selected_values_require_unique_series_and_finite_nonnegative_numbers(self):
        for number in (-1, float('inf'), float('-inf'), float('nan'), True, '0', None):
            with self.subTest(number=number):
                value = fixture()
                value['values'][0]['value'] = number
                with self.assertRaisesRegex(ValueError, 'invalid_health_counter'):
                    t.select_health_series(value, **policy())
        value = fixture()
        value['values'].append(copy.deepcopy(value['values'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_health_metric_series'):
            t.select_health_series(value, **policy())

    def test_same_metric_with_distinct_labels_is_preserved(self):
        value = fixture()
        value['values'].append({'name': 'engine:preemptions_total', 'labels': '{instance="second"}', 'value': 4})
        self.assertEqual(t.select_health_series(value, **policy())[
            ('engine:preemptions_total', '{instance="second"}')], 4)

    def test_capture_bytes_hash_and_encoding_are_checked(self):
        for field, replacement in (('bytes', 0), ('sha256', '0' * 64), ('base64', 'invalid!')):
            with self.subTest(field=field):
                value = fixture()
                value['body'][field] = replacement
                with self.assertRaises(ValueError):
                    t.select_health_series(value, **policy())
        with self.assertRaises(UnicodeDecodeError):
            t.select_health_series({'body': captured(b'\xff'), 'values': []}, name_pattern='error')


class JournalAppendTests(unittest.TestCase):
    def test_initial_append_and_unchanged_snapshot_reconstruct_exact_bytes(self):
        initial = journal_append()
        raw, cursor = t.verify_journal_append(initial, None, None)
        self.assertEqual(raw, b'{"event":"started"}\n')
        delta = b'{"event":"finished","text":"\xc3\xa9"}\n'
        value = journal_append(raw, cursor, delta)
        combined, next_cursor = t.verify_journal_append(value, raw, cursor)
        self.assertEqual(combined, raw + delta)
        self.assertEqual(t.verify_journal_append(journal_append(combined, next_cursor, b''),
                                                combined, next_cursor), (combined, next_cursor))

    def test_schema_cursor_and_offset_changes_refuse(self):
        raw, cursor = t.verify_journal_append(journal_append(), None, None)
        for field, replacement, reason in (
                ('schema', 'other', 'journal_cursor_changed'),
                ('cursor_before', None, 'journal_cursor_changed'),
                ('offset', len(raw) - 1, 'journal_append_offset')):
            with self.subTest(field=field):
                value = journal_append(raw, cursor)
                value[field] = replacement
                with self.assertRaisesRegex(ValueError, reason):
                    t.verify_journal_append(value, raw, cursor)

    def test_delta_bytes_hash_and_base64_changes_refuse(self):
        for field, replacement in (('bytes', 0), ('sha256', '0' * 64), ('base64', '?')):
            with self.subTest(field=field):
                value = journal_append()
                value[field] = replacement
                with self.assertRaises(ValueError):
                    t.verify_journal_append(value, None, None)

    def test_full_prefix_hash_length_and_torn_final_line_refuse(self):
        for field, replacement in (('bytes', 0), ('sha256', '0' * 64)):
            with self.subTest(field=field):
                value = journal_append()
                value['cursor_after'][field] = replacement
                with self.assertRaisesRegex(ValueError, 'journal_complete_prefix_pin'):
                    t.verify_journal_append(value, None, None)
        for raw in (b'', b'{"event":"started"}', b'complete\npartial'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, 'journal_complete_prefix_pin'):
                    t.verify_journal_append(journal_append(delta=raw), None, None)

    def test_rotated_journal_refuses_even_with_matching_bytes(self):
        raw, cursor = t.verify_journal_append(journal_append(), None, None)
        for changed in ({'device': 3}, {'inode': 3}):
            with self.subTest(changed=changed):
                value = journal_append(raw, cursor, b'', **changed)
                with self.assertRaisesRegex(ValueError, 'journal_identity_changed'):
                    t.verify_journal_append(value, raw, cursor)

    def test_changed_cached_prefix_refuses(self):
        raw, cursor = t.verify_journal_append(journal_append(), None, None)
        value = journal_append(raw, cursor)
        with self.assertRaisesRegex(ValueError, 'journal_complete_prefix_pin'):
            t.verify_journal_append(value, raw.replace(b'started', b'changed'), cursor)

    def test_helpers_do_not_mutate_inputs_or_open_files(self):
        value = fixture()
        receipt = journal_append()
        originals = copy.deepcopy((value, receipt))
        with patch('builtins.open', side_effect=AssertionError('unexpected I/O')):
            t.select_health_series(value, **policy())
            t.verify_journal_append(receipt, None, None)
        self.assertEqual((value, receipt), originals)


if __name__ == '__main__':
    unittest.main()
