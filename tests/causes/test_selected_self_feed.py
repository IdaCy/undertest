import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_selected_self_feed as feed
from tests.causes import test_selected_reader_feed as fixtures


class SelectedSelfFeedTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SelectedReaderFeedTests()
        fixture.setUp()
        self.docs = fixture.docs
        self.docs['reader_registration'] = fixture.registration
        self.docs['self_configuration'] = dict(self.docs['configuration'], destination='/self-saved',
                                              reader_inputs_only=False, evidence_profile='early_self_probes')
        self.registration = dict(schema='causes.selected_actor_self_feed_registration.v1',
            reader_registration='reader_registration', selection='selection', configuration='self_configuration',
            execution_host='producer', remote_base='/campaign', destination='/self-saved',
            self_probe_custody=dict(originals_retained_on_producer=True, local_copy_included=True,
                                    evidence_profile='early_self_probes'))

    def validate(self):
        return feed.validate_registration(self.registration, lambda ref: copy.deepcopy(self.docs[ref]))

    def test_exact_profile_reuses_all_producers_without_filtering_success(self):
        config = self.validate()
        self.assertEqual(config['selected_runs'], self.docs['configuration']['selected_runs'])
        self.assertFalse(config['reader_inputs_only'])
        self.assertEqual(config['evidence_profile'], 'early_self_probes')

    def test_inherited_foreign_job_refuses(self):
        self.docs['a_next64_jobs'][0] = self.docs['global'][0]
        with self.assertRaisesRegex(ValueError, 'stage_jobs'):
            self.validate()

    def test_own_destination_is_required(self):
        self.registration['destination'] = '/saved'
        self.docs['self_configuration']['destination'] = '/saved'
        with self.assertRaisesRegex(ValueError, 'self_configuration'):
            self.validate()

    def test_profile_selection_limits_and_floor_cannot_drift(self):
        for field, value in [('reader_inputs_only', True), ('evidence_profile', None),
                             ('selected_runs', []), ('limit', 9), ('free_space_floor_bytes', 0)]:
            with self.subTest(field=field):
                original = self.docs['self_configuration'][field]
                self.docs['self_configuration'][field] = value
                with self.assertRaisesRegex(ValueError, 'self_configuration'):
                    self.validate()
                self.docs['self_configuration'][field] = original

    def complete_load(self, observe=None, **changes):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            written = {}

            def save(name, value):
                path = root / (name + '.json')
                path.write_text(json.dumps(value, sort_keys=True) + '\n')
                return feed.reader.pin(path)

            def convert(value):
                if isinstance(value, dict):
                    return {k: convert(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [convert(v) for v in value]
                if isinstance(value, str) and value in self.docs:
                    if value not in written:
                        written[value] = save(value, convert(self.docs[value]))
                    return written[value]
                return value

            self.docs['self_configuration']['destination'] = str(root / 'self-evidence')
            self.registration['destination'] = str(root / 'self-evidence')
            registration = convert(self.registration)
            reg = save('registration', registration)
            repository = Path(__file__).resolve().parents[2]
            parent = root / 'parent.py'
            parent.write_text('from causes import campaign_handoff as h\n'
                              'from causes import campaign_snapshot as snapshot\n')
            sources = {key: feed.reader.pin(repository / 'src/causes' / ('campaign_' + key + '.py'))
                       for key in ['handoff', 'snapshot']}
            sources['watch'] = feed.reader.pin(repository / 'src/causes/campaign_snapshot_watch.py')
            ssh = root / 'ssh_config'
            ssh.write_text('# synthetic offline fixture\n')
            p = dict(schema='causes.selected_actor_self_feed_plan.v1', runtime_authorized=True,
                producer_plan_binding_complete=True, source=feed.reader.pin(feed.__file__),
                reader_feed_source=feed.reader.pin(feed.reader.__file__), registration=reg,
                configuration=registration['configuration'], remote_base='/campaign',
                destination=registration['destination'], transport_parent=feed.reader.pin(parent), sources=sources,
                ssh_config=feed.reader.pin(ssh), interval_seconds=180, maximum_local_copy_bytes=10_000_000_000,
                watch_directory=str(root / 'watch'))
            p.update(changes)
            plan = save('plan', p)
            review = save('review', dict(accepted_selected_actor_self_feed_preparation=True,
                          plan=plan, source=p['source'], findings=[]))
            authority = save('authority', dict(root_authorized=True, no_user_revocation=True, plan=plan,
                             review=review, maximum_invocations=1, automatic_retry=False))
            result = feed.load(plan, review, authority)
            if observe is not None:
                observe(result, root, written)
            return result

    def test_whole_load_uses_native_self_profile_and_custody(self):
        def observe(result, root, written):
            p, config, watch, authorize, parent = result
            self.assertEqual(config.index_name, 'self_probes.accepted.jsonl')
            self.assertIs(watch.snapshot, parent.snapshot)
            self.assertEqual(config.evidence_profile, 'early_self_probes')
            self.assertEqual(authorize(config)['custody_kind'], 'actor_self_probe_evidence')
        self.complete_load(observe=observe)

    def test_pending_schema_and_more_than_ten_gb_refuse_before_native_import(self):
        for changes in [dict(schema='other'), dict(runtime_authorized=False),
                        dict(producer_plan_binding_complete=False), dict(maximum_local_copy_bytes=10_000_000_001)]:
            with self.subTest(changes=changes), patch.object(feed.importlib.util, 'spec_from_file_location',
                    side_effect=AssertionError('native import reached')):
                with self.assertRaises(ValueError):
                    self.complete_load(**changes)

    def test_authorization_retains_and_rehashes_all_parent_inputs(self):
        def observe(result, root, written):
            _, config, _, authorize, _ = result
            with patch.object(feed, 'validate_registration', side_effect=AssertionError('rederived roster')):
                first = authorize(config)
                self.assertEqual(first, authorize(config))
                for name in ['reader_registration', 'global', 'a_ordinals', 'b_remaining_jobs',
                             'actor_config', 'a_remaining_review', 'self_configuration']:
                    path = Path(written[name]['path'])
                    original = path.read_bytes()
                    path.write_bytes(original + b' ')
                    try:
                        with self.subTest(name=name), self.assertRaises((ValueError, AssertionError)):
                            authorize(config)
                    finally:
                        path.write_bytes(original)
        self.complete_load(observe=observe)

    def test_local_hold_metadata_counts_toward_ten_gb(self):
        def observe(result, root, written):
            p, config, _, authorize, _ = result
            watch = Path(p['watch_directory'])
            watch.mkdir()
            with (watch / 'retained_receipts').open('wb') as stream:
                stream.truncate(10_000_000_000 - 2 * 1024**3 + 1)
            with patch.object(feed.subprocess, 'run') as transport:
                with self.assertRaisesRegex(ValueError, 'local_copy_budget_hold'):
                    authorize(config)
                transport.assert_not_called()
        self.complete_load(observe=observe)

    def test_budget_hold_before_transport_restores_native_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = object()
            snapshot = types.SimpleNamespace(subprocess=original)
            parent = types.SimpleNamespace(snapshot=snapshot, h=types.SimpleNamespace(checked_bytes=lambda r: None),
                                           strict_transport=lambda argv, p: argv)
            config = types.SimpleNamespace(destination=root / 'destination')
            p = dict(watch_directory=str(root / 'watch'), ssh_config={}, maximum_local_copy_bytes=10_000_000_000,
                     interval_seconds=180)
            watch = types.SimpleNamespace(watch_completed=lambda *a, **k: snapshot.subprocess.run(['ssh', 'fixture']))
            with patch.object(feed, 'load', return_value=(p, config, watch, None, parent)), \
                    patch.object(feed.reader, 'check_local_budget', side_effect=ValueError('local_copy_budget_hold')), \
                    patch.object(feed.subprocess, 'run') as transport:
                with self.assertRaisesRegex(ValueError, 'local_copy_budget_hold'):
                    feed.main(None, None, None)
                transport.assert_not_called()
            self.assertIs(snapshot.subprocess, original)


if __name__ == '__main__':
    unittest.main()
