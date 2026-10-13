import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_reader_input_audit as audit


class MetadataTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return audit.pin(path)

    def settings(self, duplicate=False):
        pending = self.save('pending.json', {'feeds': []})
        templates = {'materialization': pending}
        preparation = self.save('preparation.json', {'templates': templates})
        peer = {
            'accepted_fresh_reader_next_batch_metadata_preparation': True,
            'preparation': preparation,
            'findings': [],
        }
        if duplicate:
            peer['templates'] = copy.deepcopy(templates)
        return {
            'metadata_review': self.save('review.json', peer),
            'metadata_preparation': preparation,
            'pending_request': pending,
        }

    def test_minimal_and_duplicated_peer_templates_are_accepted(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                settings = self.settings(duplicate)
                self.assertEqual(audit.review_metadata_template(settings),
                                 audit.ck(settings['metadata_review']))

    def test_conflicting_duplicate_is_rejected(self):
        settings = self.settings(True)
        peer = audit.ck(settings['metadata_review'])
        peer['templates']['materialization'] = self.save('other.json', {})
        settings['metadata_review'] = self.save('bad_review.json', peer)
        with self.assertRaisesRegex(ValueError, 'metadata_template_copy_mismatch'):
            audit.review_metadata_template(settings)

    def test_unreviewed_preparation_and_wrong_pending_are_rejected(self):
        for field in ('metadata_preparation', 'pending_request'):
            with self.subTest(field=field):
                settings = self.settings()
                settings[field] = self.save('other.json', {})
                with self.assertRaisesRegex(ValueError, 'accepted_next_batch_metadata'):
                    audit.review_metadata_template(settings)

    def test_changed_pending_bytes_are_rejected(self):
        settings = self.settings()
        Path(settings['pending_request']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'saved_pin_changed'):
            audit.review_metadata_template(settings)

    def test_unaccepted_or_flagged_review_is_rejected(self):
        for mutation in ({'accepted_fresh_reader_next_batch_metadata_preparation': False},
                         {'findings': ['unresolved']}):
            settings = self.settings()
            peer = audit.ck(settings['metadata_review'])
            peer.update(mutation)
            settings['metadata_review'] = self.save('bad_review.json', peer)
            with self.assertRaisesRegex(ValueError, 'accepted_next_batch_metadata'):
                audit.review_metadata_template(settings)


if __name__ == '__main__':
    unittest.main()
