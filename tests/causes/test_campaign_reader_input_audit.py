import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from causes import campaign_reader_input_audit as audit


class SavedInputAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return audit.pin(path)

    def descriptor(self):
        empty = self.save('empty.json', {})
        result = {key: empty for key in audit.FIELDS - {'schema', 'interpreter'}}
        result.update(schema=audit.SCHEMA, interpreter=sys.executable)
        return result

    def invoke(self, descriptor, output):
        reference = self.save('descriptor.json', descriptor)
        return subprocess.run(
            [sys.executable, '-B', '-m', 'causes.campaign_reader_input_audit',
             '--descriptor', json.dumps(reference), '--output', str(output)],
            capture_output=True, text=True, env=os.environ.copy(),
        )

    def test_changed_and_aliased_files_are_rejected(self):
        ref = self.save('source.json', {'value': 1})
        Path(ref['path']).write_text('{"value": 2}')
        with self.assertRaisesRegex(ValueError, 'saved_pin_changed'):
            audit.raw(ref)
        alias = self.root / 'alias.json'
        alias.symlink_to(ref['path'])
        with self.assertRaisesRegex(ValueError, 'canonical_saved_file_required'):
            audit.pin(alias)

    def test_unlisted_descriptor_fields_are_rejected(self):
        descriptor = self.descriptor()
        descriptor['skip_failures'] = True
        with self.assertRaisesRegex(ValueError, 'exact_descriptor_required'):
            audit.review_saved_inputs(descriptor)

    def test_every_declared_evidence_pin_is_checked(self):
        descriptor = self.descriptor()
        descriptor['previous_review'] = dict(descriptor['previous_review'], sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'saved_pin_changed'):
            audit.review_saved_inputs(descriptor)

    def test_cli_preserves_existing_output(self):
        output = self.root / 'review.json'
        output.write_text('retained evidence')
        result = self.invoke(self.descriptor(), output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exclusive_review_output_required', result.stderr)
        self.assertEqual(output.read_text(), 'retained evidence')

    def test_cli_blocks_response_and_environment_file_reads(self):
        for name in ('.env', 'response.raw', 'probes/example.json'):
            with self.subTest(name=name):
                descriptor = self.descriptor()
                descriptor['previous_review'] = self.save(name, {})
                output = self.root / 'review.json'
                result = self.invoke(descriptor, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('scientific_response_read_blocked', result.stderr)
                self.assertFalse(output.exists())

    def test_request_changes_beyond_late_cut_binding_are_rejected(self):
        descriptor = self.descriptor()
        source = self.save('source.py.json', {})
        known = self.save('known.json', {})
        peer = self.save('source_review.json', {
            'accepted_fresh_candidate_reader_materializer': True,
            'source': source, 'known': known,
        })
        output = self.root / 'materialized'
        descriptor['preparation'] = self.save('materialized/PREPARATION.v1.json', {
            'source': source, 'known': known,
        })
        pending = {
            'source': source, 'known': known, 'source_review': peer,
            'output': str(output), 'root_authorized': False,
            'feeds': [{'role': 'producer', 'cut': None, 'cut_review': None}],
        }
        descriptor['pending_request'] = self.save('pending.json', pending)
        for mutation in ('authority', 'role', 'source'):
            with self.subTest(mutation=mutation):
                current = copy.deepcopy(pending)
                current['root_authorized'] = True
                current['feeds'][0].update(cut=known, cut_review=peer)
                if mutation == 'authority':
                    current['root_authorized'] = False
                elif mutation == 'role':
                    current['feeds'][0]['role'] = 'different-producer'
                else:
                    current['extra_source'] = known
                descriptor['request'] = self.save('request.json', current)
                with self.assertRaisesRegex(ValueError, 'only_late_cut_authority'):
                    audit.review_saved_inputs(descriptor)


if __name__ == '__main__':
    unittest.main()
