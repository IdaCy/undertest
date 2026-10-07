import base64
import copy
from dataclasses import replace
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes.campaign_staging import stage
from causes.campaign_staging_reuse import ReceiptContract, reduce_inventory


class StagingReuseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.contract = ReceiptContract(
            stage_step='stage-files', inventory_acceptance_field='accepted_inventory',
            orchestration_acceptance_field='accepted_stage_wrapper',
            configuration_host_field='destination_host', remote_python='/usr/bin/python3')

    def save(self, name, value, raw=False):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if raw else h.encoded(value) + b'\n')
        return h.pin(path)

    def pair(self, name, value):
        ref = self.save(name + '.json', value)
        review = self.save(name + '_review.json', {
            self.contract.inventory_acceptance_field: True, 'inventory': ref})
        return {'evidence': ref, 'review': review}

    def fixture(self, change=None):
        stager = self.save('stager.py', b'fixture stager', raw=True)
        public_stager = self.save('published/stager.py', b'fixture stager', raw=True)
        wrapper = self.save('wrapper.py', b'fixture wrapper', raw=True)
        one = self.save('already.json', {'old': 1})
        two = self.save('new.json', {'new': 1})
        protected = self.save('protected/control.json', {'keep': 1})
        scope = dict(host='fixture', base=str(self.base), protected_paths=[str(self.base / 'protected')],
                     staging_source=stager, purpose='fixture', no_overwrite_or_retry=True)
        prior = copy.deepcopy({**scope, 'copy': [one], 'verify_only': [protected]})
        full = copy.deepcopy({**scope, 'copy': [one, two], 'verify_only': [protected]})
        staging_review = self.save('staging_review.json', {
            'accepted_campaign_staging_source': True, 'source': public_stager})
        configuration = self.save('config.json', {
            self.contract.configuration_host_field: scope['host'], 'base': scope['base'],
            'protected_stage_paths': scope['protected_paths'],
            'references': {'staging_source': stager, 'staging_review': staging_review}})
        source_review = self.save('source_review.json', {
            self.contract.orchestration_acceptance_field: True,
            'source': wrapper, 'configuration': configuration})
        remote = [self.contract.remote_python, '-B', '-c', 'fixture stager', '--base', str(self.base),
                  '--protected-path', str(self.base / 'protected')]
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', 'fixture', shlex.join(remote)]
        primitive = dict(files=[{**one, 'existing_identical': False}], verify_only_count=1,
                         model_or_service_actions=False)
        execution = dict(argv=copy.deepcopy(argv), returncode=0, timed_out=False,
                         completion_unknown=False, started_at='1', ended_at='2')
        # Packet bodies are deliberately absent: only this retained pin is used.
        packet = {'path': str(self.base / 'unread_packet'), 'bytes': 900000000, 'sha256': 'a' * 64}
        intent = dict(step=self.contract.stage_step, source=wrapper, configuration=configuration,
                      source_review=source_review, no_automatic_retry=True,
                      plan=dict(argv=argv, kind='command', step=self.contract.stage_step,
                                files=[{'pin': packet}], stdin=packet))
        if change:
            change(full, prior, intent, execution, primitive)
        prior_pair = self.pair('prior', prior)
        inputs = self.save('stage_inputs.json', {'inventory': prior_pair})
        intent['inputs'] = inputs
        full_pair = self.pair('full', full)
        execution['stdout'] = self.save('stage/stdout', primitive)
        execution['stderr'] = self.save('stage/stderr', b'', raw=True)
        execution_ref = self.save('stage/EXECUTION.json', execution)
        self.save('stage/INTENT.json', intent)
        result = self.save('stage/RESULT.json', dict(
            at='3', step=self.contract.stage_step, inputs=inputs, source=wrapper,
            no_following_step_executed=True,
            result=dict(execution=execution_ref, primitive_output=primitive)))
        return dict(inventory=full_pair, prior_successful_stages=[result]), one, two, protected

    def test_exact_partition_metadata_and_read_only_boundary(self):
        inputs, one, two, protected = self.fixture()
        before = copy.deepcopy(inputs)
        file_bytes = {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        with patch('subprocess.Popen', side_effect=AssertionError('transport forbidden')), \
                patch.object(h, 'save_new', side_effect=AssertionError('writes forbidden')):
            reduced, proof = reduce_inventory(inputs, self.contract)
        self.assertEqual(inputs, before)
        self.assertEqual(file_bytes, {path: path.read_bytes() for path in self.base.rglob('*') if path.is_file()})
        self.assertEqual(reduced['copy'], [two])
        self.assertEqual(reduced['verify_only'], [protected, one])
        self.assertEqual(proof['promoted_verify_count'], 1)
        self.assertEqual(proof['remaining_copy_bytes'], two['bytes'])
        self.assertEqual(proof['moved'][0]['pin'], one)
        self.assertFalse(any(key.startswith('accepted_') for key in proof))
        original = h.checked(inputs['inventory']['evidence'])
        excluded = ('copy', 'verify_only')
        self.assertEqual({k: v for k, v in reduced.items() if k not in excluded},
                         {k: v for k, v in original.items() if k not in excluded})

    def test_failed_ambiguous_transport_and_mismatched_receipts_refuse(self):
        changes = [
            lambda f, p, i, e, o: e.update(returncode=255),
            lambda f, p, i, e, o: e.update(completion_unknown=True),
            lambda f, p, i, e, o: e.update(timed_out=True),
            lambda f, p, i, e, o: o['files'][0].update(sha256='b' * 64),
            lambda f, p, i, e, o: i['plan']['argv'].__setitem__(-2, 'other-host'),
            lambda f, p, i, e, o: o.update(verify_only_count=0),
        ]
        for number, change in enumerate(changes):
            with self.subTest(case=number):
                inputs, *_ = self.fixture(change)
                with self.assertRaises(ValueError):
                    reduce_inventory(inputs, self.contract)

    def test_changed_pin_scope_or_overlapping_inventory_refuses(self):
        changes = [
            lambda f, p, i, e, o: f['copy'][0].update(sha256='b' * 64),
            lambda f, p, i, e, o: f.update(host='other'),
            lambda f, p, i, e, o: f['verify_only'].append(f['copy'][0]),
            lambda f, p, i, e, o: f.update(protected_paths=[]),
            lambda f, p, i, e, o: f['copy'][0].update(bytes=float(f['copy'][0]['bytes'])),
        ]
        for number, change in enumerate(changes):
            with self.subTest(case=number):
                inputs, *_ = self.fixture(change)
                with self.assertRaises(ValueError):
                    reduce_inventory(inputs, self.contract)

    def test_unknown_file_stays_copy_and_duplicate_receipt_refuses(self):
        inputs, _, two, _ = self.fixture()
        reduced, _ = reduce_inventory(inputs, self.contract)
        self.assertEqual(reduced['copy'], [two])
        inputs['prior_successful_stages'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate_stage_receipt'):
            reduce_inventory(inputs, self.contract)

    def test_contract_matches_the_actual_producer(self):
        inputs, *_ = self.fixture()
        changes = dict(stage_step='another-step', inventory_acceptance_field='another_flag',
                       orchestration_acceptance_field='another_flag',
                       configuration_host_field='another_host_field', remote_python='/bin/python3')
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises((ValueError, KeyError)):
                reduce_inventory(inputs, replace(self.contract, **{field: value}))
        with self.assertRaisesRegex(ValueError, 'invalid_receipt_contract'):
            replace(self.contract, stage_step='')

    def test_public_source_and_execution_snapshot_must_both_match(self):
        inputs, *_ = self.fixture()
        reduce_inventory(inputs, self.contract)
        (self.base / 'published/stager.py').write_bytes(b'changed source')
        with self.assertRaises(ValueError):
            reduce_inventory(inputs, self.contract)

    def test_promoted_destination_is_rechecked_before_any_new_copy(self):
        inputs, one, two, _ = self.fixture()
        reduced, _ = reduce_inventory(inputs, self.contract)
        destination = Path(two['path'])
        raw = destination.read_bytes()
        destination.unlink()
        packet = {'files': [{'destination': two['path'], 'bytes': two['bytes'],
                             'sha256': two['sha256'], 'base64': base64.b64encode(raw).decode()}],
                  'verify_only': reduced['verify_only']}
        promoted = Path(one['path'])
        promoted.unlink()
        with self.assertRaises(FileNotFoundError):
            stage(packet, self.base, reduced['protected_paths'])
        self.assertFalse(destination.exists())
        promoted.write_bytes(b'changed destination')
        with self.assertRaises(ValueError):
            stage(packet, self.base, reduced['protected_paths'])
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
