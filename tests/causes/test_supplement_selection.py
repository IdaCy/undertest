import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_selection as selection


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.counter = 0

    def save(self, value):
        self.counter += 1
        path = self.root / f'{self.counter}.json'
        path.write_bytes(core.encoded(value))
        return core.pin(path)

    def jobs(self, family='first', offset=0):
        return [dict(family=family, model='model', run_id=f'run{i + offset}',
                     stage='early', question_id='strong', seed=41 + i,
                     measurement_id=f'{family}/model/run{i + offset}/early/strong',
                     request={'messages': [{'role': 'user', 'content': f'task {i}'}]})
                for i in range(4)]

    def source(self, jobs, ordinals=(1, 3)):
        ref = self.save(jobs)
        rows = [dict(ordinal=i, measurement_id=jobs[i]['measurement_id'],
                     input_sha256=core.digest(jobs[i])) for i in ordinals]
        return dict(jobs=ref, selection=self.save(dict(jobs=ref, rows=rows)))

    def load(self, sources, occupied_ids=()):
        return selection.load_selected_groups(
            self.save(dict(schema=selection.SCHEMA, sources=sources)),
            occupied_ids=occupied_ids)

    def change_rows(self, source, modify):
        value = json.loads(core.pinned(source['selection']))
        modify(value['rows'])
        return {**source, 'selection': self.save(value)}

    def test_groups_preserve_sources_jobs_seeds_and_order(self):
        a, b, c = self.jobs(), self.jobs('second'), self.jobs(offset=10)
        groups = self.load([self.source(a), self.source(b, (0,)), self.source(c, (2,))])
        self.assertEqual(groups, [dict(family='first', model='model', jobs=[a[1], a[3], c[2]]),
                                  dict(family='second', model='model', jobs=[b[0]])])

    def test_any_occupied_attempt_is_refused_not_filtered(self):
        jobs = self.jobs()
        for state in ('failed', 'active', 'completed', 'ambiguous'):
            with self.subTest(state=state), self.assertRaisesRegex(
                    core.IntegrityError, 'selection_occupied_attempt'):
                self.load([self.source(jobs)], occupied_ids=[jobs[1]['measurement_id']])

    def test_changed_seed_still_rejected_with_updated_file_pins(self):
        source = self.source(self.jobs())
        original = json.loads(core.pinned(source['selection']))
        jobs = json.loads(core.pinned(source['jobs']))
        jobs[1]['seed'] += 1
        changed = self.save(jobs)
        original['jobs'] = changed
        with self.assertRaisesRegex(core.IntegrityError, 'selection_original_job_changed'):
            self.load([dict(jobs=changed, selection=self.save(original))])

    def test_bare_occupied_string_cannot_silently_become_characters(self):
        jobs = self.jobs()
        mid = jobs[1]['measurement_id']
        for value in (mid, mid.encode(), {mid: 'failed'}):
            with self.subTest(value=value), self.assertRaisesRegex(
                    core.IntegrityError, 'selection_occupied_ids_must_be_collection'):
                self.load([self.source(jobs)], occupied_ids=value)

    def test_source_pin_binding_and_file_tampering(self):
        source = self.source(self.jobs())
        mismatch = {**source, 'jobs': self.save(self.jobs('second'))}
        with self.assertRaisesRegex(core.IntegrityError, 'selection_source_binding'):
            self.load([mismatch])
        Path(source['jobs']['path']).write_text('[]')
        with self.assertRaisesRegex(core.IntegrityError, 'file_pin_mismatch'):
            self.load([source])

    def test_ordinals_must_be_original_unique_order(self):
        source = self.source(self.jobs())
        for ordinal in (-1, True, 4, '1'):
            with self.subTest(ordinal=ordinal), self.assertRaisesRegex(
                    core.IntegrityError, 'selection_original_ordinal_order'):
                self.load([self.change_rows(source, lambda rows: rows[0].update(ordinal=ordinal))])
        for modify in (lambda rows: rows.reverse(), lambda rows: rows.append(copy.deepcopy(rows[0]))):
            with self.assertRaisesRegex(core.IntegrityError, 'selection_original_ordinal_order'):
                self.load([self.change_rows(source, modify)])

    def test_duplicate_ids_across_sources_and_repeated_source_refused(self):
        jobs = self.jobs()
        with self.assertRaisesRegex(core.IntegrityError, 'selection_duplicate_selected_id'):
            self.load([self.source(jobs), self.source(jobs)])
        source = self.source(jobs)
        with self.assertRaisesRegex(core.IntegrityError, 'selection_repeated_source'):
            self.load([source, source])

    def test_duplicate_and_mixed_original_inventory_refused(self):
        jobs = self.jobs()
        jobs[2] = copy.deepcopy(jobs[0])
        with self.assertRaisesRegex(core.IntegrityError, 'selection_duplicate_original_id'):
            self.load([self.source(jobs)])
        jobs[2] = self.jobs('second')[2]
        with self.assertRaisesRegex(core.IntegrityError, 'selection_mixed_source_scope'):
            self.load([self.source(jobs)])

    def test_identity_mismatch_and_empty_selection_refused(self):
        jobs = self.jobs()
        jobs[0]['measurement_id'] = 'other/model/run0/early/strong'
        with self.assertRaisesRegex(core.IntegrityError, 'selection_job_identity'):
            self.load([self.source(jobs)])
        with self.assertRaisesRegex(core.IntegrityError, 'selection_rows'):
            self.load([self.source(self.jobs(), ())])


if __name__ == '__main__':
    unittest.main()
