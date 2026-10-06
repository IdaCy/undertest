from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest

from causes.corpus_allocation import admissible_cells, chosen_alias, digest, key
from causes.task_bank import load_task


BANK = Path(__file__).resolve().parents[2] / 'data/causes/task_bank/v1'


class PublishedBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((BANK / 'manifest.json').read_bytes())
        cls.catalog = json.loads((BANK / 'catalog.json').read_bytes())
        cls.holds = json.loads((BANK / 'holds.json').read_bytes())
        cls.tasks = {p.stem: p for p in (BANK / 'tasks').glob('*.json')}
        cls.bundles = [json.loads(p.read_bytes()) for p in (BANK / 'variants').rglob('*.json')]

    def test_exact_file_inventory_and_version_counts(self):
        pins = self.manifest['files']
        paths = [row['path'] for row in pins]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {str(p.relative_to(BANK)) for p in BANK.rglob('*')
                                     if p.is_file() and p.name != 'manifest.json'})
        for row in pins:
            relative = PurePosixPath(row['path'])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn('..', relative.parts)
            raw = (BANK / relative).read_bytes()
            self.assertEqual(len(raw), row['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['sha256'])
        self.assertEqual(len(self.tasks), self.manifest['tasks'])
        self.assertEqual(len(self.bundles), self.manifest['source_versions'])
        self.assertEqual((len(self.tasks), len(self.bundles)), (83, 84))
        self.assertFalse(self.manifest['contains_generated_answers'])

    def test_all_eligible_stimuli_aliases_and_held_cells(self):
        physical = defaultdict(list)
        counts = Counter()
        sources = {}
        for bundle in self.bundles:
            self.assertNotIn(bundle['source_id'], sources)
            sources[bundle['source_id']] = bundle
            self.assertIn(bundle['task_id'], self.tasks)
            lookup = {row['id']: row for row in bundle['variants']}
            self.assertEqual(len(lookup), len(bundle['variants']))
            for index, row in enumerate(bundle['variants']):
                self.assertEqual(row['task_id'], bundle['task_id'])
                counts['all'] += 1
                counts['eligible' if row['eligible'] else 'ineligible'] += 1
                if not row['eligible']:
                    continue
                stimulus = digest({field: row.get(field) for field in ('request', 'history', 'workspace', 'files', 'env')})
                self.assertEqual(stimulus, row['stimulus_sha256'])
                if row.get('alias_of'):
                    canonical = lookup[row['alias_of']]
                    self.assertTrue(canonical['eligible'])
                    self.assertEqual(canonical['stimulus_sha256'], stimulus)
                physical[(bundle['task_id'], stimulus)].append({
                    'source_id': bundle['source_id'], 'row_index': index, 'variant_id': row['id'],
                    'definition_sha256': row['definition_sha256'], 'factor': row['factor'],
                    'level': row['level'], 'alias_of': row.get('alias_of'),
                })
        self.assertEqual(counts, {'all': 4820, 'eligible': 4540, 'ineligible': 280})
        cells = self.catalog['physical_cells']
        self.assertEqual(len(cells), len(physical))
        self.assertEqual({key(cell) for cell in cells}, set(physical))
        held = {(row['task_id'], row['stimulus_sha256']): row['hold_id'] for row in self.holds['holds']}
        self.assertEqual(len(held), 15)
        for cell in cells:
            self.assertCountEqual(cell['source_aliases'], physical[key(cell)])
            self.assertIn(chosen_alias(cell), cell['source_aliases'])
            self.assertEqual(cell['hold_ids'], [held[key(cell)]] if key(cell) in held else [])
        self.assertEqual(len(admissible_cells(self.catalog)), 4477)

    def test_all_task_paths_bind_to_explicit_fixture_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for task_id, path in self.tasks.items():
                value = json.loads(path.read_bytes())
                self.assertEqual(value['task_id'], task_id)
                for relative in value['assets'].values():
                    if relative is not None:
                        (root / relative).mkdir(parents=True, exist_ok=True)
                result = load_task(path, root)
                for field, original in value['spec'].items():
                    self.assertEqual(result[field], original)
                self.assertEqual(hashlib.sha256(result['request'].encode()).hexdigest(), result['request_sha256'])


if __name__ == '__main__':
    unittest.main()
