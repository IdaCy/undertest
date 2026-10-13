"""Integrity and compatibility checks for the complete formal rewrite bank."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import unittest

from causes.task_generation import bind_variant_identities


ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / 'data/causes/task_bank/fully_formal_v1'


class FormalTaskBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((BANK / 'manifest.json').read_bytes())
        cls.bundles = [json.loads(path.read_bytes())
                       for path in sorted((BANK / 'variants').glob('*.json'))]

    def test_complete_file_inventory_and_baseline_manifest(self):
        files = self.manifest['files']
        self.assertEqual(len(files), 83)
        self.assertEqual({row['path'] for row in files},
                         {str(path.relative_to(BANK)) for path in BANK.rglob('*')
                          if path.is_file() and path.name != 'manifest.json'})
        for row in files:
            relative = PurePosixPath(row['path'])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn('..', relative.parts)
            body = (BANK / relative).read_bytes()
            self.assertEqual(len(body), row['bytes'])
            self.assertEqual(hashlib.sha256(body).hexdigest(), row['sha256'])
        original = ROOT / self.manifest['baseline_bank_path'] / 'manifest.json'
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),
                         self.manifest['baseline_manifest_sha256'])

    def test_every_pair_preserves_the_exact_original_baseline(self):
        tasks = set()
        for bundle in self.bundles:
            self.assertNotIn(bundle['task_id'], tasks)
            tasks.add(bundle['task_id'])
            source = bundle['baseline_source']
            body = (ROOT / source['path']).read_bytes()
            self.assertEqual(len(body), source['bytes'])
            self.assertEqual(hashlib.sha256(body).hexdigest(), source['sha256'])
            original = json.loads(body)
            baseline = next(row for row in original['variants'] if row['id'] == 'B00L00')
            self.assertEqual(bundle['variants'][0], baseline)
            self.assertEqual(bundle['source_sha256'], original['source_sha256'])
            task = json.loads((ROOT / bundle['task_spec_path']).read_bytes())
            self.assertEqual(task['task_id'], bundle['task_id'])
        self.assertEqual(len(tasks), 83)

    def test_distinct_native_formal_identities_without_legacy_replacement(self):
        for bundle in self.bundles:
            rows = bundle['variants']
            self.assertEqual([row['id'] for row in rows], ['B00L00', 'F03L06'])
            self.assertEqual(bind_variant_identities(rows), rows)
            formal = rows[1]
            self.assertEqual(formal['task_id'], bundle['task_id'])
            self.assertEqual(formal['semantic_version'], 'fully_formal_v1')
            self.assertEqual(formal['intervention'], 'whole_formal_rewrite')
            self.assertTrue(formal['eligible'])
            self.assertNotIn('alias_of', formal)
            self.assertNotEqual(formal['request'], rows[0]['request'])
            self.assertNotEqual(formal['stimulus_sha256'], rows[0]['stimulus_sha256'])
        self.assertFalse(self.manifest['contains_generated_answers'])
        self.assertFalse(self.manifest['isolated_formality_effect'])
        self.assertFalse(self.manifest['ordinal_dose_claim'])


if __name__ == '__main__':
    unittest.main()
