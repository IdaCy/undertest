import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from causes.recipe_controls.eligibility import CONDITION_FIELDS
from causes.recipe_controls.eligibility import FAMILY_ABSENCE
from causes.recipe_controls.eligibility import INDEX_ISSUES
from causes.recipe_controls.eligibility import REQUIRED_REFERENCES
from causes.recipe_controls.eligibility import checked_bytes
from causes.recipe_controls.eligibility import derive_family_exception
from causes.recipe_controls.eligibility import digest
from causes.recipe_controls.eligibility import durable_inventory
from causes.recipe_controls.eligibility import strict_json
from causes.recipe_controls.eligibility import unavailable_population
from causes.recipe_controls.eligibility import verify_source_archive


def fixture():
    expected = {'assignment_id': 'assignment', 'instance_id': 'ledger-dev-0', 'family': 'ledger',
                'framing': 'ordinary', 'grading': 'limited', 'rollout': 0, 'prompt_variant': 'base',
                'condition': 'ordinary-limited', 'recipe_version': 'recipe-v1', 'rubric_version': 'rubric-v1'}
    assignment = {key: value for key, value in expected.items() if key not in ('family', 'condition', 'recipe_version', 'rubric_version')}
    pair = {'system': 'System π', 'user': 'User é'}
    messages = [{'role': 'system', 'content': pair['system']}, {'role': 'user', 'content': pair['user']},
                {'role': 'assistant', 'content': 'Readable unlinked text.'}]
    message_bytes = (json.dumps(messages, ensure_ascii=False, indent=2) + '\n').encode()
    ref = {'path': '/saved/messages.json', 'bytes': len(message_bytes), 'sha256': hashlib.sha256(message_bytes).hexdigest()}
    refs = {key: dict(ref) for key in REQUIRED_REFERENCES}
    run = {'run_id': 'run', 'config_id': 'config', 'model_revision': 'revision', 'section': 'effe', 'run_type': 'pilot', 'model': 'provider/model'}
    run_metadata = {'undertest': {key: value for key, value in run.items() if key != 'model'}}
    inspect_identity = {'inspect_run_id': 'inspectrun', 'eval_id': 'eval', 'task_id': 'task', 'eval_created': '2026-09-30T00:00:00Z'}
    ledger_path = '/original/ledgers/assignment-attempt.jsonl'
    identity = {'assignment_id': 'assignment', 'attempt_id': 'attempt', 'ledger_path': ledger_path}
    start = {'phase': 'start', 'assignment_id': 'assignment', 'attempt_id': 'attempt',
             'started_at_utc': '2026-09-30T00:00:00+00:00', 'metadata': dict(expected)}
    ledger_bytes = (json.dumps(start) + '\n' + json.dumps({'phase': 'cleanup', 'assignment_id': 'assignment', 'attempt_id': 'attempt'}) + '\n').encode()
    ledger_ref = {'path': '/saved/assignment-attempt.jsonl', 'bytes': len(ledger_bytes), 'sha256': hashlib.sha256(ledger_bytes).hexdigest()}
    attempts = durable_inventory([{'raw': ledger_bytes, 'reference': ledger_ref}])['assignment']
    native = {'metadata_integrity': True, 'metadata_complete': False, 'metadata_conflicts': [], 'metadata_absent': copy.deepcopy(FAMILY_ABSENCE)}
    native_attempt = {**copy.deepcopy(native), 'attempt_id': 'attempt', 'started': True, 'started_at': start['started_at_utc'],
                      'order_source': 'started_at', 'ledger_integrity': True, 'errors': []}
    reconciled = {**assignment, **copy.deepcopy(native), 'primary_order_verified': True, 'primary_attempt_id': 'attempt', 'attempts': [native_attempt]}
    canonical = {**ref, 'status': 'readable'}
    indexed = {'assignment_id': 'assignment', 'primary_attempt_id': 'attempt', 'condition_metadata': dict(assignment),
               'assignment_provenance': copy.deepcopy(native), 'condition_attribution_trusted': False, 'unattributed_attempts': [],
               'primary_evidence': {'preserved_attempt_fields': copy.deepcopy(native), 'condition_attribution_trusted': False,
                   'verified_first_attempt': True, 'attempt_id': 'attempt', 'attribution_issues': list(INDEX_ISSUES),
                   'snapshots': [{'transcript': {'status': 'available', 'schema_issues': []}, 'sources': {'messages.json': canonical}}]}}
    projected = {'assignment_id': 'assignment', 'verified_attempt_id': 'attempt', 'original_index_row': copy.deepcopy(indexed),
                 'annotation_id': 'annotation', 'native_linkage_eligible': False, 'family_eligibility_override_applied': False,
                 'selection_issues': [], 'canonical_source': canonical}
    sample = {'id': 'assignment', 'uuid': 'uuid', 'epoch': 1, 'metadata': dict(expected), 'input': pair['user'], 'messages': messages,
              'output': {'model': 'model', 'choices': []}, 'events': [{'event': 'model', 'model': run['model'], 'output': {'model': 'model', 'choices': []}}],
              'store': {'effects_instance_id': 'ledger-dev-0', 'effects_attempt_id': 'attempt', 'effects_ledger_path': ledger_path,
                        'artifacts': {'effects/identity.json': json.dumps(identity)}}}
    csv_row = {**run, **inspect_identity, 'sample_id': 'assignment', 'condition': 'ordinary-limited', 'epoch': '1',
               'sample_metadata': json.dumps(expected), 'eval_metadata': json.dumps(run_metadata), 'log_metadata': json.dumps(run_metadata),
               'sample_key': 'key', 'artifacts': '/original/artifacts/key', 'log': '/original/logs/messages.json',
               'artifact_export_errors': '[]', 'artifact_error': ''}
    request = {'model': 'model', 'messages': messages[:2]}
    sample['events'][0].update(input=copy.deepcopy(messages[:2]), call={'request': copy.deepcopy(request), 'response': {'model': 'model', 'choices': []}})
    sample['events'].insert(0, {'event': 'sample_init', 'sample': {'id': 'assignment', 'input': pair['user'], 'metadata': copy.deepcopy(expected)},
                             'state': {'metadata': copy.deepcopy(expected), 'output': {'model': run['model'], 'choices': []}, 'store': {}, 'messages': []}})
    return {'assignment_id': 'assignment', 'expected_metadata': expected, 'canonical_family': 'ledger',
            'assignment': assignment, 'reproduced_assignment': copy.deepcopy(assignment), 'references': refs, 'global_issues': [],
            'manifest': {**run, 'schedule_sha256': 'schedulehash', 'input_sha256': {'assignment': digest(pair)}},
            'expected_run': run, 'reproduced_schedule_sha256': 'schedulehash', 'input_pair': pair, 'rendered_input_pair': copy.deepcopy(pair),
            'raw_eval_identity': {'model': run['model'], **inspect_identity, 'metadata': copy.deepcopy(run_metadata), 'log_metadata': copy.deepcopy(run_metadata)},
            'expected_inspect_identity': inspect_identity, 'sample': sample, 'csv_row': csv_row,
            'export_metadata': {'sample': dict(expected), 'eval': copy.deepcopy(run_metadata), 'log': copy.deepcopy(run_metadata)},
            'export_identity': identity, 'export_messages': message_bytes, 'canonical_messages': message_bytes, 'export_errors': [],
            'model_requests': [request], 'served_model': 'model',
            'durable_attempts': attempts, 'original_run_path': '/original', 'reconciled': reconciled, 'indexed': indexed, 'projected_owner': projected}


class EligibilityTests(unittest.TestCase):
    def assert_refused(self, evidence):
        result = derive_family_exception(evidence)
        self.assertEqual(result['qualification_disposition'], 'unavailable', result)
        self.assertTrue(result['refusal_reasons'])
        self.assertIs(result['treatment_linkage_eligible'], False)
        self.assertIsNone(result['review_resolution'])

    def test_source_preparation_never_grants_linkage_or_mutates_input(self):
        evidence = fixture()
        before = copy.deepcopy(evidence)
        result = derive_family_exception(evidence)
        self.assertEqual(result['qualification_disposition'], 'qualified_candidate_pending_independent_review', result['refusal_reasons'])
        self.assertFalse(result['treatment_linkage_eligible'])
        self.assertIsNone(result['review_resolution'])
        self.assertFalse(result['native_flags_rewritten'])
        self.assertEqual(evidence, before)

    def test_unavailable_receipt_preserves_actual_native_flags(self):
        evidence = fixture()
        evidence['reconciled']['metadata_integrity'] = False
        result = derive_family_exception(evidence)
        self.assertEqual(result['qualification_disposition'], 'unavailable')
        self.assertIs(result['original_native_flags']['metadata_integrity'], False)
        evidence = fixture()
        evidence['projected_owner']['original_index_row']['condition_metadata']['rollout'] = False
        self.assert_refused(evidence)

    def test_each_required_condition_field_missing_or_conflicting_refuses(self):
        for container_path in [('sample', 'metadata'), ('export_metadata', 'sample'), ('durable_attempts', 0, 'records', 0, 'metadata')]:
            for field in CONDITION_FIELDS:
                for operation in ('missing', 'conflicting'):
                    with self.subTest(container=container_path, field=field, operation=operation):
                        evidence = fixture()
                        value = evidence
                        for key in container_path:
                            value = value[key]
                        if operation == 'missing':
                            del value[field]
                        else:
                            value[field] = True if field == 'rollout' else 'conflict'
                        self.assert_refused(evidence)

    def test_missing_assignment_fields_and_family_present_refuse(self):
        for field in fixture()['assignment']:
            evidence = fixture()
            del evidence['assignment'][field]
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['assignment']['family'] = 'ledger'
        self.assert_refused(evidence)

    def test_all_present_metadata_identity_fields_checked(self):
        for container in ('manifest', 'sample', 'export_metadata', 'raw_eval_identity', 'csv_row'):
            evidence = fixture()
            evidence[container]['instance_id'] = 'conflicting-instance'
            self.assert_refused(evidence)
        for field in ('effects_instance_id', 'effects_attempt_id', 'effects_ledger_path'):
            evidence = fixture()
            evidence['sample']['store'][field] = 'conflicting'
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['store']['effects_assessment'] = {'result': {'instance_id': 'conflicting'}}
        self.assert_refused(evidence)

    def test_every_required_reference_must_be_exactly_shaped(self):
        for field in REQUIRED_REFERENCES:
            evidence = fixture()
            del evidence['references'][field]
            self.assert_refused(evidence)
            evidence = fixture()
            evidence['references'][field]['bytes'] = True
            self.assert_refused(evidence)

    def test_original_inputs_and_every_model_call_prefix_checked(self):
        for side in ('system', 'user'):
            evidence = fixture()
            evidence['input_pair'][side] += ' changed'
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['model_requests'].append({'model': 'model', 'messages': [{'role': 'system', 'content': 'changed'}, {'role': 'user', 'content': 'User é'}]})
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['model_requests'] = []
        self.assert_refused(evidence)

    def test_flags_issues_and_attempt_conflicts_refuse(self):
        for scope in ('reconciled', 'indexed'):
            evidence = fixture()
            native = evidence[scope] if scope == 'reconciled' else evidence[scope]['assignment_provenance']
            native['metadata_absent'].append({'source': 'metadata', 'fields': ['framing']})
            self.assert_refused(evidence)
        for field, value in [('metadata_integrity', False), ('metadata_complete', True), ('metadata_conflicts', ['conflict'])]:
            evidence = fixture()
            evidence['reconciled'][field] = value
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['indexed']['primary_evidence']['attribution_issues'].append('unavailable_or_invalid_transcript')
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['reconciled']['primary_order_verified'] = False
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['durable_attempts'].append(copy.deepcopy(evidence['durable_attempts'][0]))
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['durable_attempts'][0]['records'][0]['phase'] = 'cleanup'
        self.assert_refused(evidence)

    def test_transcript_corruption_formatting_and_canonical_identity_refuse(self):
        for key in ('export_messages', 'canonical_messages'):
            evidence = fixture()
            evidence[key] += b' '
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['projected_owner']['canonical_source']['path'] = '/other/messages.json'
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['indexed']['primary_evidence']['snapshots'][0]['transcript']['schema_issues'] = ['corrupt']
        self.assert_refused(evidence)

    def test_capped_evidence_does_not_become_full_capture(self):
        evidence = fixture()
        evidence['sample']['limit'] = {'type': 'token', 'limit': 24000}
        result = derive_family_exception(evidence)
        self.assertEqual(result['qualification_disposition'], 'qualified_candidate_pending_independent_review')
        self.assertNotIn('full_channel_capture', result)
        self.assertFalse(result['treatment_linkage_eligible'])

    def test_unreadable_or_missing_required_evidence_refuses(self):
        for name in ('sample', 'durable_attempts', 'reconciled', 'projected_owner', 'export_metadata'):
            evidence = fixture()
            del evidence[name]
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['global_issues'] = ['unidentified_ledger']
        self.assert_refused(evidence)

    def test_run_revision_and_all_run_scopes_refuse_conflict(self):
        for name in ('manifest', 'csv_row'):
            evidence = fixture()
            evidence[name]['model_revision'] = 'other'
            self.assert_refused(evidence)

    def test_every_recorded_model_surface_refuses_conflict(self):
        for path in [('sample', 'output'), ('sample', 'messages', 2), ('sample', 'events', 1), ('sample', 'events', 1, 'output')]:
            evidence = fixture()
            value = evidence
            for part in path:
                value = value[part]
            value['model'] = 'different-model'
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['events'][1]['output']['choices'] = [{'message': {'model': 'different-model'}}]
        self.assert_refused(evidence)

    def test_raw_response_and_durable_output_models_refuse_conflict(self):
        for value in (None, {"model": "other", "choices": []}, {"choices": []}):
            evidence = fixture()
            evidence['sample']['events'][1]['call']['response'] = value
            self.assert_refused(evidence)
        evidence = fixture()
        evidence['durable_attempts'][0]['records'][-1]['model_output'] = {'model': 'other', 'choices': []}
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['durable_attempts'][0]['records'][-1]['model_output'] = {'model': 'model', 'choices': [{'message': {'model': 'other'}}]}
        self.assert_refused(evidence)

    def test_initial_conditions_strict_types_and_initial_model_role(self):
        for side in ('sample', 'state'):
            for field in CONDITION_FIELDS:
                evidence = fixture()
                evidence['sample']['events'][0][side]['metadata'][field] = True if field == 'rollout' else 'other'
                self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['events'][0]['state']['output']['model'] = 'model'
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['events'][0]['state']['output']['choices'] = [{'message': {'model': 'model'}}]
        self.assert_refused(evidence)

    def test_recorded_event_inputs_and_identity_copies_refuse_conflict(self):
        evidence = fixture()
        evidence['sample']['events'][1]['input'].append({'role': 'assistant', 'content': 'text', 'model': 'other'})
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['reconciled']['attempts'][0]['metadata_sources'] = [{'present': True, 'value': {'metadata': {'rollout': False}}}]
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['events'].append({'event': 'store', 'changes': [{'op': 'add', 'path': '/effects_assessment', 'value': {'result': {'instance_id': 'other'}}}]})
        self.assert_refused(evidence)
        evidence = fixture()
        evidence['sample']['events'].append({'event': 'state', 'changes': [{'op': 'add', 'path': '/messages/2', 'value': {'model': 'other'}}]})
        self.assert_refused(evidence)

    def test_initial_to_served_model_transition_is_role_specific(self):
        evidence = fixture()
        evidence['sample']['events'].append({'event': 'state', 'changes': [{'op': 'replace', 'path': '/output/model', 'replaced': 'provider/model', 'value': 'model'}]})
        self.assertEqual(derive_family_exception(evidence)['qualification_disposition'], 'qualified_candidate_pending_independent_review')
        evidence['sample']['events'][-1]['changes'][0]['replaced'] = 'model'
        self.assert_refused(evidence)

    def test_model_looking_actor_text_is_not_identity_evidence(self):
        evidence = fixture()
        evidence['sample']['events'][1]['call']['response']['choices'] = [{'message': {'role': 'assistant', 'content': '{"model":"fictional", "framing":"other"}'}}]
        self.assertEqual(derive_family_exception(evidence)['qualification_disposition'], 'qualified_candidate_pending_independent_review')
        evidence = fixture()
        evidence['sample']['events'] = []
        self.assert_refused(evidence)
        for top, nested in [('raw_eval_identity', 'metadata'), ('raw_eval_identity', 'log_metadata'), ('export_metadata', 'eval'), ('export_metadata', 'log')]:
            evidence = fixture()
            evidence[top][nested]['undertest']['run_id'] = 'other'
            self.assert_refused(evidence)

    def test_strict_json_duplicate_and_nonfinite_refusal(self):
        for raw in ('{"x":1,"x":1}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                strict_json(raw)

    def test_source_bytes_identity_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source'
            path.write_bytes(b'source')
            ref, raw = checked_bytes(path)
            self.assertEqual(raw, b'source')
            path.write_bytes(b'change')
            with self.assertRaises(ValueError):
                checked_bytes(path, ref)
            link = Path(temporary) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                checked_bytes(link)
            with self.assertRaises(ValueError):
                checked_bytes(path, root=Path(temporary) / 'elsewhere')
            with self.assertRaises(ValueError):
                checked_bytes(Path(temporary) / 'sub' / '..' / 'source', root=Path(temporary) / 'sub')

    def test_global_failure_retains_all_fixed_rows_without_linkage(self):
        population = [{'assignment_id': 'assignment-' + str(index)} for index in range(160)]
        rows = unavailable_population(population, 'unreadable source archive', {})
        self.assertEqual([row['assignment_id'] for row in rows], [row['assignment_id'] for row in population])
        self.assertTrue(all(row['qualification_disposition'] == 'unavailable' and row['treatment_linkage_eligible'] is False for row in rows))
        with self.assertRaises(ValueError):
            unavailable_population([{'assignment_id': 'duplicate'}, {'assignment_id': 'duplicate'}], 'missing', {})

    def test_source_zip_inventory_and_member_bytes_refuse_changes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('src/code.py', b'x=1\n')
        hashes = {'src/code.py': hashlib.sha256(b'x=1\n').hexdigest()}
        self.assertEqual(verify_source_archive(buffer.getvalue(), hashes), {'src/code.py': b'x=1\n'})
        for bad in ({}, {'src/code.py': '0' * 64}, {'other.py': hashes['src/code.py']}):
            with self.assertRaises(ValueError):
                verify_source_archive(buffer.getvalue(), bad)

    def test_rejected_headers_timestamps_and_identity_duplicates(self):
        base = fixture()['durable_attempts'][0]['records'][0]
        for record in [None, {'phase': 'cleanup'}, {**base, 'started_at_utc': None}, {**base, 'started_at_utc': '2026-09-30T00:00:00'}, {**base, 'started_at_utc': '2026-09-30T00:00:00+03:00'}, {**base, 'attempt_id': ''}]:
            raw = (json.dumps(record) + '\n').encode()
            ref = {'path': '/x', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            with self.assertRaises(ValueError):
                durable_inventory([{'raw': raw, 'reference': ref}])
        raw = (json.dumps(base) + '\n').encode()
        ref = {'path': '/x', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        entry = {'raw': raw, 'reference': ref}
        with self.assertRaises(ValueError):
            durable_inventory([entry, entry])
        second = {**base, 'attempt_id': 'second'}
        other = (json.dumps(second) + '\n').encode()
        with self.assertRaises(ValueError):
            durable_inventory([entry, {'raw': other, 'reference': {'path': '/y', 'bytes': len(other), 'sha256': hashlib.sha256(other).hexdigest()}}])


if __name__ == '__main__':
    unittest.main()
