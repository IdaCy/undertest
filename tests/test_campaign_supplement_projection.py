import copy
import unittest

from causes.campaign_supplement_projection import (
    PRESENCE, QUESTIONS, digest, project_verified_supplement_pairs,
)


def ref(name):
    return dict(path='/evidence/' + name, bytes=10, sha256='a' * 64)


def inputs(runs=('run-a', 'run-b')):
    jobs, members, verified, evidence = [], [], [], {}
    for run in runs:
        for question in QUESTIONS:
            mid = run + '/' + question
            job = dict(measurement_id=mid, run_id=run, stage='early', question_id=question,
                       seed=47, context=ref(run + '/context'), context_canonical_sha256='b' * 64,
                       question_canonical_sha256='c' * 64, request_body_sha256='d' * 64,
                       request_file_sha256='a' * 64)
            final = ref(mid + '/FINAL.json')
            captures = [ref(mid + '/' + name) for name in
                        ('request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json')]
            member = dict(measurement_id=mid, run_id=run, stage='early', question_id=question,
                          context=job['context'], epoch='epoch-1', readout='self_' + question,
                          global_job_sha256=digest(job), final=final, capture_files=captures,
                          request=captures[0], terminal=dict(measurement_id=mid, final=final,
                              technical_status='complete', event='route_success'))
            native = dict(measurement_id=mid, disposition='final_verified', integrity_failure=False,
                          input_canonical_sha256=digest(job), final=final, capture_files=captures,
                          technical_status='complete', admissible_label='yes', label='yes',
                          raw_parsed_label='yes', raw_result=dict(answer='yes', technical_status='complete'),
                          offline_verification='exact_frozen_request_render_and_parser_reconstructed',
                          reason=None, failure=None)
            for key in ('context', 'context_canonical_sha256', 'question_canonical_sha256',
                        'seed', 'request_body_sha256'):
                native[key] = job[key]
            evidence[mid] = dict(measurement_id=mid, final=final, request_sha256=job['request_body_sha256'],
                                 verified_finals=ref('verified.json'), verified_index=len(verified),
                                 native_collector=ref('collector.py'), custody=dict(
                                     actual=ref('cut.json'), review=ref('peer.json'),
                                     membership=ref('membership.json'), closed_run_id=run,
                                     terminal=True, accepted_flag='accepted_saved_cut'))
            jobs.append(job)
            members.append(member)
            verified.append(native)
    return dict(jobs=jobs, epochs={run: 'epoch-1' for run in runs},
                terminal_membership=members, verified_finals=verified,
                evidence_by_measurement=evidence,
                occupied_rows=[dict(run_id=run, readout=PRESENCE, status='observed',
                                    label='no', reason=None, evidence={'original': True},
                                    epoch='epoch-1') for run in runs])


def make_missing(data, index=0, parsed=False):
    value = data['verified_finals'][index]
    value.update(technical_status='parse_failure' if parsed else 'request_failure',
                 admissible_label=None, label=None, reason='preserved failure', failure=None,
                 raw_result={'technical_status': 'parse_failure', 'answer': 'invalid'} if parsed else None,
                 raw_parsed_label='invalid' if parsed else None,
                 offline_verification='exact_frozen_request_render_and_parser_reconstructed'
                     if parsed else 'technical_missing_no_parsed_result')
    data['terminal_membership'][index]['terminal'].update(
        technical_status=value['technical_status'], event='route_failure')


class SupplementProjectionTests(unittest.TestCase):
    def test_pair_schema_and_no_input_mutation(self):
        data = inputs()
        before = copy.deepcopy(data)
        result = project_verified_supplement_pairs(**data)
        self.assertEqual(result['counts'], dict(contexts=2, occupied=4, observed=4, missing=0))
        self.assertEqual(result['terminal_run_ids'], ['run-a', 'run-b'])
        self.assertEqual(result['existing_presence'], data['occupied_rows'])
        self.assertEqual(data, before)
        row = result['slots'][0]
        self.assertEqual(set(row), {'run_id', 'readout', 'status', 'label', 'reason', 'evidence', 'epoch'})
        row['evidence']['custody']['terminal'] = False
        result['existing_presence'][0]['evidence']['original'] = False
        self.assertEqual(data, before)

    def test_technical_and_parser_failures_remain_occupied_missing(self):
        for parsed in (False, True):
            with self.subTest(parsed=parsed):
                data = inputs(('run-a',))
                make_missing(data, parsed=parsed)
                result = project_verified_supplement_pairs(**data)
                self.assertEqual(result['counts'], dict(contexts=1, occupied=2, observed=1, missing=1))
                self.assertEqual(result['slots'][0]['reason'], 'preserved failure')
                self.assertIsNone(result['slots'][0]['label'])

    def test_missing_presence_is_preserved_and_never_replaced(self):
        data = inputs(('run-a',))
        data['occupied_rows'][0].update(status='missing', label=None, reason='old failure')
        result = project_verified_supplement_pairs(**data)
        self.assertEqual(result['existing_presence'], data['occupied_rows'])
        self.assertEqual(len(result['slots']), 2)

    def test_missing_parsed_answer_keeps_its_exact_json_type(self):
        for answer, parsed in ((1, True), (1, 1.0), (False, 0)):
            with self.subTest(answer=answer, parsed=parsed):
                data = inputs(('run-a',))
                make_missing(data, parsed=True)
                data['verified_finals'][0]['raw_result']['answer'] = answer
                data['verified_finals'][0]['raw_parsed_label'] = parsed
                with self.assertRaisesRegex(ValueError, 'missing_parsed_identity'):
                    project_verified_supplement_pairs(**data)

    def test_incomplete_group_is_returned_pending_even_when_positive(self):
        data = inputs(('run-a',))
        removed = data['terminal_membership'].pop()
        data['verified_finals'].pop()
        data['evidence_by_measurement'].pop(removed['measurement_id'])
        result = project_verified_supplement_pairs(**data)
        self.assertEqual(result['slots'], [])
        self.assertEqual(result['pending_incomplete_membership'], data['terminal_membership'])

    def test_occupied_supplement_is_rejected_even_in_incomplete_group(self):
        data = inputs(('run-a',))
        removed = data['terminal_membership'].pop()
        data['verified_finals'].pop()
        data['evidence_by_measurement'].pop(removed['measurement_id'])
        data['occupied_rows'].append(dict(run_id='run-a', readout='self_' + QUESTIONS[0]))
        with self.assertRaisesRegex(ValueError, 'cannot_replay'):
            project_verified_supplement_pairs(**data)

    def test_duplicates_are_not_silently_collapsed(self):
        for field in ('jobs', 'terminal_membership', 'verified_finals', 'occupied_rows'):
            with self.subTest(field=field):
                data = inputs()
                data[field].append(copy.deepcopy(data[field][0]))
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    project_verified_supplement_pairs(**data)

    def test_exact_native_job_request_context_and_label_joins(self):
        mutations = [
            ('input_canonical_sha256', 'f' * 64), ('seed', 48),
            ('context', ref('other')), ('context_canonical_sha256', 'f' * 64),
            ('question_canonical_sha256', 'f' * 64), ('request_body_sha256', 'f' * 64),
            ('final', ref('other-final')), ('integrity_failure', True),
            ('disposition', 'not_verified'), ('label', 'no'), ('raw_parsed_label', 'no'),
            ('admissible_label', 'invented'), ('offline_verification', 'unchecked'),
            ('raw_result', {'answer': 'no', 'technical_status': 'complete'}),
        ]
        for field, value in mutations:
            with self.subTest(field=field):
                data = inputs()
                data['verified_finals'][0][field] = value
                with self.assertRaises(ValueError):
                    project_verified_supplement_pairs(**data)

    def test_boolean_or_float_seed_cannot_equal_an_integer_seed(self):
        for impostor in (True, 1.0):
            with self.subTest(impostor=impostor):
                data = inputs(('run-a',))
                job = data['jobs'][0]
                job['seed'] = 1
                data['terminal_membership'][0]['global_job_sha256'] = digest(job)
                data['verified_finals'][0]['input_canonical_sha256'] = digest(job)
                data['verified_finals'][0]['seed'] = impostor
                with self.assertRaisesRegex(ValueError, 'integer_seed_required'):
                    project_verified_supplement_pairs(**data)

    def test_equal_numeric_reference_values_do_not_hide_type_changes(self):
        # Separate each source's reference to avoid fixture aliases hiding mutations.
        for target in ('context', 'final', 'terminal_final', 'capture_files', 'request',
                       'evidence_final', 'verified_context'):
            with self.subTest(target=target):
                data = inputs(('run-a',))
                job = data['jobs'][0]
                member = data['terminal_membership'][0]
                value = data['verified_finals'][0]
                evidence = data['evidence_by_measurement'][job['measurement_id']]
                if target == 'verified_context':
                    value['context'] = dict(value['context'], bytes=10.0)
                elif target == 'evidence_final':
                    evidence['final'] = dict(evidence['final'], bytes=10.0)
                elif target == 'terminal_final':
                    member['terminal']['final'] = dict(member['terminal']['final'], bytes=10.0)
                elif target == 'capture_files':
                    member['capture_files'] = copy.deepcopy(member['capture_files'])
                    member['capture_files'][0]['bytes'] = 10.0
                else:
                    member[target] = dict(member[target], bytes=10.0)
                with self.assertRaises(ValueError):
                    project_verified_supplement_pairs(**data)

    def test_membership_cannot_change_epoch_instrument_or_request(self):
        for field, value in [('epoch', 'other'), ('readout', PRESENCE),
                             ('global_job_sha256', 'f' * 64), ('request', ref('other')),
                             ('question_id', 'evaluation_presence'), ('stage', 'late')]:
            with self.subTest(field=field):
                data = inputs()
                data['terminal_membership'][0][field] = value
                with self.assertRaises(ValueError):
                    project_verified_supplement_pairs(**data)

    def test_evidence_must_name_the_exact_verification_and_attempt(self):
        for field, value in [('verified_index', True), ('verified_index', 99),
                             ('measurement_id', 'foreign'), ('final', ref('foreign')),
                             ('request_sha256', 'f' * 64)]:
            with self.subTest(field=field, value=value):
                data = inputs()
                data['evidence_by_measurement'][data['jobs'][0]['measurement_id']][field] = value
                with self.assertRaises(ValueError):
                    project_verified_supplement_pairs(**data)

    def test_missing_unknown_or_pending_presence_refused(self):
        for kind in ('missing', 'pending', 'epoch'):
            with self.subTest(kind=kind):
                data = inputs(('run-a',))
                if kind == 'missing':
                    data['occupied_rows'] = []
                elif kind == 'pending':
                    data['occupied_rows'][0]['status'] = 'pending'
                else:
                    data['occupied_rows'][0]['epoch'] = 'other'
                with self.assertRaises(ValueError):
                    project_verified_supplement_pairs(**data)

    def test_verified_or_evidence_membership_must_be_exact(self):
        for field in ('verified_finals', 'evidence_by_measurement'):
            data = inputs()
            if field == 'verified_finals':
                data[field].pop()
            else:
                data[field].pop(next(iter(data[field])))
            with self.assertRaises(ValueError):
                project_verified_supplement_pairs(**data)

    def test_global_job_order_controls_projection(self):
        data = inputs()
        expected = project_verified_supplement_pairs(**data)
        data['terminal_membership'].reverse()
        self.assertEqual(project_verified_supplement_pairs(**data), expected)

    def test_failure_cannot_carry_an_admissible_label_or_fake_parser(self):
        for key, value in [('admissible_label', 'yes'), ('raw_result', {'answer': 'yes'}),
                           ('offline_verification', 'exact_frozen_request_render_and_parser_reconstructed')]:
            data = inputs()
            make_missing(data)
            data['verified_finals'][0][key] = value
            with self.assertRaises(ValueError):
                project_verified_supplement_pairs(**data)


if __name__ == '__main__':
    unittest.main()
