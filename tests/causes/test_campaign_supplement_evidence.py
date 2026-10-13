import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_evidence as storage
from causes import campaign_supplement_finite as finite
from causes import campaign_supplement_launch as launch
from causes.campaign_monitor import replay_history
from shared import evidence_bytes as exact
from tests.causes import test_campaign_supplement_finite as queue_fixture


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = b'{"event":"controller_started"}\n'
        path = self.root / 'baseline'
        path.write_bytes(self.base)
        self.scope = dict(id='actor-route', identity=dict(host='host-a', output='/actor-a'),
                          baseline=exact.pin(path), mode='live_prefix', model_aliases=['model:route'],
                          accepted_state={k: replay_history(self.base)[k] for k in launch.HISTORY_FIELDS})

    def observation(self, ordinal=1):
        return dict(identity=dict(provider='fixed-provider'),
                    observed_from=f'2026-01-01T00:{ordinal // 60:02}:{ordinal % 60:02}+00:00',
                    observed_until=f'2026-01-01T00:{ordinal // 60:02}:{ordinal % 60:02}+00:00')

    def write(self, name, raw, ordinal=1, previous=None):
        return storage.write_capture(self.root / name, raw, scope=self.scope,
                                     observation=self.observation(ordinal), previous=previous)

    def altered(self, reference, edit, name='altered'):
        value = json.loads(exact.read_pinned(reference['wrapper']))
        edit(value)
        path = self.root / (name + '.json')
        path.write_bytes(core.encoded(value) + b'\n')
        return dict(schema=storage.REFERENCE_SCHEMA, wrapper=exact.pin(path))

    def test_raw_default_preserves_pin_and_does_not_guess_gzip(self):
        raw = gzip.compress(b'arbitrary saved bytes')
        path = self.root / 'raw'
        ref = storage.write_transport(path, raw, identity={'stream': 'stdout'})
        self.assertEqual(ref, exact.pin(path))
        self.assertEqual(storage.read_transport(ref, identity={'stream': 'stdout'}), raw)

    def test_gzip_roundtrip_preserves_noncanonical_intent_and_partial_stdout(self):
        for name, raw in [('intent', b'{ "argv" : ["one", "two"] }\n'),
                          ('stdout', b'partial\0\xff\n{"unfinished":')]:
            identity = dict(observation='one', stream=name)
            ref = storage.write_transport(self.root / (name + '.gz'), raw,
                                          identity=identity, encoding='gzip')
            self.assertEqual(storage.read_transport(ref, identity=identity), raw)
            with self.assertRaisesRegex(ValueError, 'identity_changed'):
                storage.read_transport(ref, identity=dict(identity, observation='two'))

    def test_unknown_encoding_refuses_before_write(self):
        target = self.root / 'unknown'
        with self.assertRaisesRegex(ValueError, 'unsupported_transport'):
            storage.write_transport(target, b'a', identity={'stream': 'stdout'}, encoding='auto')
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, 'transport_reference_schema'):
            storage.read_transport({'schema': 'guess', 'path': str(target)}, identity={'stream': 'stdout'})

    def test_transport_trailing_truncated_or_changed_gzip_refuses(self):
        raw = b'full bytes' * 100
        ref = storage.write_transport(self.root / 'good.gz', raw,
                                      identity={'stream': 'stdout'}, encoding='gzip')
        packed = exact.read_pinned(ref['payload']['stored'])
        for number, bad in enumerate((packed + b'junk', packed + packed, packed[:-1])):
            path = self.root / f'bad-{number}.gz'
            path.write_bytes(bad)
            changed = copy.deepcopy(ref)
            changed['payload']['stored'] = exact.pin(path)
            with self.subTest(number=number), self.assertRaises(ValueError):
                storage.read_transport(changed, identity={'stream': 'stdout'})
        with self.assertRaises(ValueError):
            storage.read_transport(ref, identity={'stream': 'stdout'}, limit=len(raw) - 1)

    def test_distinct_empty_observations_retain_distinct_records(self):
        first = self.write('one', self.base)
        second = self.write('two', self.base, 2, first)
        self.assertNotEqual(first, second)
        value = json.loads(exact.read_pinned(second['wrapper']))
        self.assertEqual(value['previous'], first)
        self.assertEqual(storage.read_capture(self.scope, second, self.observation(2)), self.base)
        with self.assertRaisesRegex(ValueError, 'repeated_observation'):
            self.write('repeated', self.base, 2, second)
        self.assertFalse((self.root / 'repeated').exists())

    def test_rollover_caps_flat_group_and_preserves_entire_previous_prefix(self):
        previous = None
        raw = self.base
        for ordinal in range(1, 67):
            raw += core.encoded(dict(event='progress', ordinal=ordinal)) + b'\n'
            ref = self.write(f'capture-{ordinal}', raw, ordinal, previous)
            value = json.loads(exact.read_pinned(ref['wrapper']))
            manifest = json.loads(exact.read_pinned(value['manifest']))
            self.assertLessEqual(len(manifest['segments']), 64)
            self.assertEqual(len(manifest['segments']), (ordinal - 1) % 64 + 1)
            self.assertEqual(storage.read_capture(self.scope, ref, self.observation(ordinal)), raw)
            if ordinal == 64:
                group_end = ref
            if ordinal == 65:
                rollover = ref
                self.assertEqual(value['previous'], group_end)
                self.assertIsNone(manifest['previous'])
            previous = ref
        changed = self.altered(rollover, lambda value: value.update(previous=None))
        with self.assertRaisesRegex(ValueError, 'initial_predecessor'):
            storage.read_capture(self.scope, changed, self.observation(65))

    def test_rollover_changed_equal_length_predecessor_refuses(self):
        previous = None
        for ordinal in range(1, 65):
            previous = self.write(f'capture-{ordinal}', self.base + b'original\n', ordinal, previous)
        with self.assertRaisesRegex(ValueError, 'previous_whole_prefix'):
            self.write('changed', self.base + b'different\n', 65, previous)
        self.assertFalse((self.root / 'changed').exists())

    def test_rollover_checks_both_immediate_parent_pin_shapes(self):
        previous = None
        for ordinal in range(1, 66):
            previous = self.write(f'shape-{ordinal}', self.base, ordinal, previous)
        child = json.loads(exact.read_pinned(previous['wrapper']))
        parent = json.loads(exact.read_pinned(child['previous']['wrapper']))
        bad_pins = [dict(path=False, bytes=True, sha256=None),
                    dict(path='relative', bytes=0, sha256='0'*64),
                    dict(path=str(self.root/'absent'), bytes=-1, sha256='0'*64),
                    dict(path=str(self.root/'absent'), bytes=0, sha256='x'*64)]
        for field in ('previous_wrapper', 'manifest_previous'):
            for number, bad_pin in enumerate(bad_pins):
                modified = copy.deepcopy(parent)
                if field == 'previous_wrapper':
                    modified['previous']['wrapper'] = bad_pin
                else:
                    manifest = json.loads(exact.read_pinned(modified['manifest']))
                    manifest['previous'] = bad_pin
                    path = self.root/f'bad-manifest-{number}.json'
                    path.write_bytes(core.encoded(manifest))
                    modified['manifest'] = exact.pin(path)
                path = self.root/f'bad-parent-{field}-{number}.json'
                path.write_bytes(core.encoded(modified))
                changed = self.altered(previous, lambda value: value.update(
                    previous=dict(schema=storage.REFERENCE_SCHEMA, wrapper=exact.pin(path))),
                    f'bad-child-{field}-{number}')
                with self.subTest(field=field, number=number), self.assertRaises(ValueError):
                    storage.read_capture(self.scope, changed, self.observation(65))

    def test_wrong_observation_scope_baseline_and_ordinal_refuse(self):
        ref = self.write('one', self.base)
        for field in ('identity', 'observed_from', 'observed_until'):
            observed = self.observation()
            observed[field] = {'provider': 'other'} if field == 'identity' else 'different'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'observation_changed'):
                storage.read_capture(self.scope, ref, observed)
        for field, value in [('id', 'other'), ('identity', {'host': 'other'}), ('mode', 'sealed')]:
            scope = dict(self.scope, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                storage.read_capture(scope, ref, self.observation())
        for number, ordinal in enumerate((0, True, 2)):
            bad = self.altered(ref, lambda value: value.update(ordinal=ordinal), f'ordinal-{number}')
            with self.subTest(ordinal=ordinal), self.assertRaises(ValueError):
                storage.read_capture(self.scope, bad, self.observation())

    def test_detached_same_group_parent_and_modified_segment_refuse(self):
        first = self.write('one', self.base)
        second = self.write('two', self.base + b'{"event":"progress"}\n', 2, first)
        other = self.write('other', self.base, 9)
        detached = self.altered(second, lambda value: value.update(previous=other))
        with self.assertRaisesRegex(ValueError, 'detached_predecessor'):
            storage.read_capture(self.scope, detached, self.observation(2))
        manifest = json.loads(exact.read_pinned(json.loads(exact.read_pinned(second['wrapper']))['manifest']))
        Path(manifest['segments'][-1]['file']['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            storage.read_capture(self.scope, second, self.observation(2))

    def test_full_native_history_keeps_failure_after_later_success(self):
        raw = self.base + b'{"event":"route_failure","route":"model:route","consecutive_failures":1}\n'
        raw += b'{"event":"route_success","routes":["model:route"]}\n'
        ref = self.write('failed', raw)
        row = dict(id=self.scope['id'], identity=self.scope['identity'], capture=ref)
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
            launch.shared_histories([self.scope], [row], replay_history,
                                     capture_reader=storage.read_capture, observation=self.observation())
        scope = dict(self.scope, history_progression='append_only_lifetime_v1')
        self.assertEqual(launch.shared_histories([scope], [row], replay_history,
            capture_reader=storage.read_capture, observation=self.observation()), 0)
        self.assertEqual(replay_history(storage.read_capture(scope, ref, self.observation()))
                         ['lifetime_route_failures'], 1)


class FiniteDecoderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = queue_fixture.FiniteSupplementTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.previous = None
        self.observations = 0
        self.change_time = False
        self.wrong_pin = False

    def observe(self):
        f = self.fixture
        value = f.observe()
        self.observations += 1
        self.previous = storage.write_capture(f.root / f'compact-{self.observations}',
            f.guard.baseline.read_bytes(), scope=f.policy['donors'][0],
            observation=value, previous=self.previous)
        value['donors'][0]['capture'] = self.previous
        if self.change_time:
            value['identity'] = {'changed': True}
        return value

    def run_role(self, with_reader=True):
        f = self.fixture
        callbacks = dict(worker=f.worker, observation=self.observe, source_check=f.sources,
                         request_check=f.request, replay_history=replay_history)
        if with_reader:
            callbacks['capture_reader'] = storage.read_capture
        refs = {key: core.pin(Path(value.__code__.co_filename).resolve())
                for key, value in callbacks.items()}
        if self.wrong_pin:
            refs['capture_reader'] = refs['observation']
        return finite.run_finite_queue(f.partition_ref, 'a', f.descriptors, f.output,
            f.policy, f.contract, **callbacks, callback_sources=refs, required_pins=[],
            pause_paths=f.policy['local_pause_paths'])

    def test_native_queue_uses_pinned_decoder_and_keeps_original_attempts(self):
        f = self.fixture
        self.assertEqual(self.run_role()['new_final'], 2)
        self.assertEqual(self.run_role()['existing_final'], 2)
        self.assertEqual(f.calls, [job['measurement_id'] for job in f.jobs[4:6]])

    def test_opt_in_does_not_relax_actor_priority(self):
        self.fixture.wait_after_first = True
        result = self.run_role()
        self.assertEqual((result['new_final'], result['remaining_unclaimed']), (1, 1))
        self.assertIn('actor_priority_metrics', result['stop_reason'])

    def test_wrong_decoder_pin_refuses_before_any_observation_or_claim(self):
        self.wrong_pin = True
        with self.assertRaisesRegex(core.IntegrityError, 'callback_source:capture_reader'):
            self.run_role()
        self.assertEqual(self.observations, 0)
        self.assertFalse(self.fixture.calls)

    def test_compact_reference_without_opt_in_is_not_silently_decoded(self):
        result = self.run_role(with_reader=False)
        self.assertEqual(result['new_final'], 0)
        self.assertFalse(self.fixture.calls)
        self.assertIsNotNone(result['stop_reason'])


if __name__ == '__main__':
    unittest.main()
