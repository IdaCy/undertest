import copy
import unittest
from causes import campaign_reader as reader
from causes.reader_operational_classification import citation_only_missing


def fixture():
    context = 'a' * 64
    result = {'context_sha256': context, 'instruments': {
        name: {'instrument': name, 'status': 'validated', 'context_sha256': context}
        for name in reader.INSTRUMENTS}}
    evidence = {}
    for name in reader.INSTRUMENTS:
        expected = {k: ('a' if k == 'context_sha256' else 'b') * 64 for k in
                    ('context_sha256', 'request_sha256', 'source_sha256', 'rubric_sha256',
                     'threshold_sha256', 'schema_sha256', 'config_sha256')}
        attempt = {**expected, 'instrument': name, 'started_at': 'synthetic', 'max_retries': 0, 'store': False}
        receipt = {**attempt, 'status': 'technical_missing', 'api_call_performed': True,
                   'http_status': 200, 'response_status': 'completed', 'response_sha256': 'c' * 64}
        evidence[name] = dict(expected=expected, attempt=attempt, receipt=receipt, actual_request_sha256=expected['request_sha256'], actual_response_sha256='c' * 64)
    return result, evidence


def invalidate(result, name='presence', code='evidence_not_exact_channel_substring:evaluation'):
    result['instruments'][name].update(status='technical_missing', technical_reason='ValueError', validation_error=code)


class CitationClassificationTests(unittest.TestCase):
    def test_completed_citation_missing_is_narrow_and_does_not_mutate(self):
        result, evidence = fixture(); invalidate(result)
        before = copy.deepcopy((result, evidence))
        self.assertEqual(citation_only_missing(result, evidence), ('presence',))
        self.assertEqual((result, evidence), before)

    def test_all_three_citation_failures_are_one_annotation_class(self):
        result, evidence = fixture()
        for name in reader.INSTRUMENTS: invalidate(result, name)
        self.assertEqual(citation_only_missing(result, evidence), reader.INSTRUMENTS)

    def test_all_native_types_and_partial_observation(self):
        for kind in reader.TYPES:
            result, evidence = fixture(); invalidate(result, code='evidence_not_exact_channel_substring:' + kind)
            result['instruments']['strongly_favors']['status'] = 'validated_partial_observation'
            self.assertEqual(citation_only_missing(result, evidence), ('presence',))

    def test_success_has_no_annotation_exemption(self):
        result, evidence = fixture(); self.assertEqual(citation_only_missing(result, evidence), ())

    def test_mixed_transport_failure_uses_normal_policy(self):
        result, evidence = fixture(); invalidate(result)
        result['instruments']['strongly_favors'].update(status='technical_missing', technical_reason='APITimeoutError')
        self.assertEqual(citation_only_missing(result, evidence), ())

    def test_other_or_unknown_validation_failure_uses_normal_policy(self):
        for reason in ['positive_evidence_is_quoted_or_code_only:evaluation', 'evidence_not_exact_channel_substring:invented',
                       'evidence_not_exact_channel_substring:evaluation extra', 'evidence_not_generated_actor:evaluation']:
            result, evidence = fixture(); invalidate(result, code=reason)
            self.assertEqual(citation_only_missing(result, evidence), ())

    def test_http_completion_required(self):
        for field, value in [('http_status', 500), ('http_status', True), ('response_status', 'incomplete'), ('api_call_performed', False)]:
            result, evidence = fixture(); invalidate(result); evidence['presence']['receipt'][field] = value
            self.assertEqual(citation_only_missing(result, evidence), ())

    def test_request_context_source_and_prompt_mismatch_refuse(self):
        for field in fixture()[1]['presence']['expected']:
            result, evidence = fixture(); invalidate(result); evidence['presence']['receipt'][field] = 'd' * 64
            with self.assertRaisesRegex(ValueError, 'evidence_mismatch'): citation_only_missing(result, evidence)

    def test_native_attempt_extension_and_result_identity_required(self):
        for target, field, value in [('attempt', 'started_at', 'changed'), ('row', 'instrument', 'other'), ('row', 'context_sha256', 'd' * 64)]:
            result, evidence = fixture(); invalidate(result)
            (result['instruments']['presence'] if target == 'row' else evidence['presence']['attempt'])[field] = value
            with self.assertRaisesRegex(ValueError, 'evidence_mismatch'): citation_only_missing(result, evidence)

    def test_missing_response_pin_refuses(self):
        result, evidence = fixture(); invalidate(result); evidence['presence']['receipt'].pop('response_sha256')
        with self.assertRaisesRegex(ValueError, 'response_pin_missing'): citation_only_missing(result, evidence)

    def test_integrity_failure_never_exempted(self):
        result, evidence = fixture(); invalidate(result); result['integrity_failure'] = True
        self.assertEqual(citation_only_missing(result, evidence), ())

    def test_unexpected_membership_and_untrusted_expected_refuse(self):
        result, evidence = fixture(); result['instruments'].pop('presence')
        with self.assertRaisesRegex(ValueError, 'membership'): citation_only_missing(result, evidence)
        result, evidence = fixture(); invalidate(result); evidence['presence']['expected']['context_sha256'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'expected_evidence'): citation_only_missing(result, evidence)

    def test_actual_request_and_raw_bytes_must_match(self):
        for field in ['actual_request_sha256', 'actual_response_sha256']:
            result, evidence = fixture(); invalidate(result); evidence['presence'][field] = 'd' * 64
            with self.assertRaises(ValueError): citation_only_missing(result, evidence)

    def test_judgments_not_read(self):
        class Forbidden:
            def __deepcopy__(self, memo): raise AssertionError('judgments read')
            def __iter__(self): raise AssertionError('judgments read')
        result, evidence = fixture(); invalidate(result)
        for row in result['instruments'].values(): row['judgments'] = Forbidden()
        self.assertEqual(citation_only_missing(result, evidence), ('presence',))


if __name__ == '__main__': unittest.main()
