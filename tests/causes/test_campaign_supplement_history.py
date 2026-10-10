import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes import campaign_monitor as monitor
from causes import campaign_supplement as core
from causes import campaign_supplement_history as history
from causes import campaign_supplement_launch as launcher


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def raw_bundle(value):
    return encoded(value) + b'\n'


def pin(path, raw=b'synthetic evidence'):
    return {'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def embedded(path, raw):
    return {'pin': pin(path, raw), 'base64': base64.b64encode(raw).decode()}


def seal_batches(batches):
    value = {'schema': history.SCHEMA, 'family': 'point8_test', 'model': 'model_a',
             'model_route': 'model:synthetic', 'config': pin('/frozen/config.json'),
             'batches': [copy.deepcopy(batch) for batch in batches]}
    prior = None
    for batch in value['batches']:
        batch['previous_batch_sha256'] = prior
        prior = hashlib.sha256(encoded(batch)).hexdigest()
    return value


def make_batch(name, segments, *, size=4, residual=0, migrated=False):
    """Synthetic native journals, preserving their real pilot/continue shapes."""
    root = '/batches/' + name
    prepared = pin('/prepared/' + name + '.json')
    old_launcher = pin('/sources/launcher_old.py')
    identity = {'family': 'point8_test', 'model': 'model_a', 'prepared': prepared,
                'core': pin('/sources/core.py'), 'runner': pin('/sources/runner.py'),
                'launcher': old_launcher}
    measurements = [{'measurement_id': f'point8_test/model_a/{name}_{i}/early/question',
                     'input_sha256': hashlib.sha256(f'{name}:{i}'.encode()).hexdigest()}
                    for i in range(size)]
    ids = [m['measurement_id'] for m in measurements]
    events, invocations, claimed, terminals = [], [], set(), set()
    for number, segment in enumerate(segments):
        phase, initial, actions = segment
        policy = {'prepared': prepared, 'output_root': root, 'initial_failures': initial,
                  'phase': phase, 'max_clients': 2, 'pilot_measurement_ids': ids[:2],
                  'launcher_source': pin('/sources/launcher_new.py') if migrated else old_launcher}
        if migrated:
            policy['launcher_succession'] = pin('/proofs/' + name + '_succession.json')
        invocations.append({'started_line': len(events) + 1,
                            'policy': embedded(f'/policies/{name}_{number}.json', raw_bundle(policy)),
                            'adoption': pin(f'/proofs/{name}_{number}_adoption.json'),
                            'review': pin(f'/proofs/{name}_{number}_review.json')})
        scope = set(ids[:2] if phase == 'pilot' else ids)
        existing = len(scope & terminals)
        residual = max(initial, residual)
        events.append({'event': 'controller_started', 'phase': phase, 'existing_finals': existing,
                       'pending': len(scope - claimed), 'inherited_failures': residual})
        completed = []
        for action, index in actions:
            mid = ids[index]
            if action == 'claim':
                events.append({'event': 'claimed', 'measurement_id': mid,
                               'input_sha256': measurements[index]['input_sha256'], 'phase': phase})
                claimed.add(mid)
            elif action in ('success', 'failure'):
                residual = 0 if action == 'success' else residual + 1
                events.append({'event': 'route_' + action, 'measurement_id': mid,
                               'technical_status': 'complete' if action == 'success' else 'failure',
                               'consecutive_failures': residual,
                               'final': pin(root + '/' + '/'.join(mid.split('/')[1:]) + '/FINAL.json')})
                terminals.add(mid); completed.append(mid)
            elif action == 'quarantine':
                events.append({'event': 'route_quarantined', 'measurement_id': mid,
                               'reason': 'integrity_failure'})
            elif action == 'unresolved':
                events.append({'event': 'unresolved_current_attempt', 'measurement_id': mid,
                               'error': 'synthetic recorder failure'})
            else:
                raise AssertionError(action)
        events.append({'event': 'controller_finished', 'phase': phase, 'existing_final': existing,
                       'new_final': len(completed), 'remaining_unclaimed': len(scope - claimed),
                       'consecutive_failures': residual, 'active': 0, 'completed_ids': completed})
    raw = b''.join(raw_bundle(event) for event in events)
    return {'output_root': root, 'identity': embedded(root + '/.control/IDENTITY.json', raw_bundle(identity)),
            'prepared': prepared, 'journal': embedded(root + '/.control/journal.jsonl', raw),
            'measurements': measurements, 'invocations': invocations,
            'evidence': {key: pin(f'/proofs/{name}_{key}.json')
                         for key in ('custody', 'export', 'retirement', 'pause')},
            'ambiguous_attempts': [], 'previous_batch_sha256': None}


def alter_journal(batch, edit):
    raw = base64.b64decode(batch['journal']['base64'])
    rows = list(map(json.loads, raw.splitlines()))
    edit(rows)
    batch['journal'] = embedded(batch['journal']['pin']['path'], b''.join(raw_bundle(r) for r in rows))


class NativeHistoryTests(unittest.TestCase):
    def replay(self, *batches):
        return history.replay_native_history(raw_bundle(seal_batches(batches)))

    def test_seed_only_does_not_invent_lifetime_failures(self):
        batch = make_batch('seed', [('continue', 2, [])])
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 0)
        self.assertEqual(result['nonzero_failures'], {'model:synthetic': 2})
        self.assertEqual(result['unresolved_events'], 0)
        self.assertEqual(result['batches'][0]['pending'], [m['measurement_id'] for m in batch['measurements']])
        self.assertFalse(result['physical_custody_certified'])
        self.assertFalse(result['runtime_authorized'])

    def test_cumulative_failure_success_and_empty_batch(self):
        first = make_batch('first', [('continue', 0, [('claim', 0), ('failure', 0)])])
        empty = make_batch('empty', [], residual=1)
        last = make_batch('last', [('continue', 0, [('claim', 0), ('success', 0)])], residual=1)
        result = self.replay(first, empty, last)
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual([b['inherited_residual_before_batch'] for b in result['batches']], [0, 1, 1])
        self.assertEqual([b['residual_after_batch'] for b in result['batches']], [1, 1, 0])
        self.assertEqual([b['actual_new_failures'] for b in result['batches']], [1, 0, 0])

    def test_pilot_repeated_continue_and_reviewed_launcher_succession(self):
        batch = make_batch('restarted', [
            ('pilot', 0, [('claim', 0), ('success', 0), ('claim', 1), ('success', 1)]),
            ('continue', 0, [('claim', 2), ('success', 2)]),
            ('continue', 0, [('claim', 3), ('success', 3)]),
        ], migrated=True)
        result = self.replay(batch)
        self.assertEqual(result['event_counts']['controller_started'], 3)
        self.assertEqual(result['batches'][0]['terminal'], 4)
        self.assertEqual(result['batches'][0]['pending'], [])
        self.assertEqual(result['unresolved_events'], 0)
        broken = copy.deepcopy(batch)
        policy = json.loads(base64.b64decode(broken['invocations'][1]['policy']['base64']))
        del policy['launcher_succession']
        broken['invocations'][1]['policy'] = embedded('/policies/restarted_1.json', raw_bundle(policy))
        with self.assertRaises(KeyError):
            self.replay(broken)

    def test_original_terminal_order_can_differ_from_claim_order(self):
        batch = make_batch('parallel', [('continue', 0, [
            ('claim', 0), ('claim', 1), ('failure', 1), ('success', 0)])])
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual(result['unresolved_events'], 0)

    def test_quarantine_remains_after_already_active_success(self):
        batch = make_batch('quarantined', [('continue', 0, [
            ('claim', 0), ('claim', 1), ('failure', 0), ('quarantine', 0), ('success', 1)])])
        result = self.replay(batch, make_batch('empty', []))
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual(set(result['quarantines']), {'model:synthetic'})
        self.assertEqual(result['quarantine_evidence'][0]['reason'], 'integrity_failure')
        self.assertEqual(result, self.replay(batch, make_batch('empty', [])))
        alter_journal(batch, lambda rows: rows.insert(-1, {'event': 'route_requalified'}))
        with self.assertRaisesRegex(ValueError, 'unsupported_native_history_event'):
            self.replay(batch)

    def test_unmatched_reported_ambiguous_unresolved_and_tail_are_not_lost(self):
        batch = make_batch('partial', [('continue', 0, [('claim', 0), ('unresolved', 0)])])
        batch['ambiguous_attempts'] = [batch['measurements'][1]['measurement_id']]
        raw = base64.b64decode(batch['journal']['base64']) + b'{"event":'
        batch['journal'] = embedded(batch['journal']['pin']['path'], raw)
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 0)
        self.assertEqual(result['unresolved_events'], 3)
        self.assertEqual(result['unconsumed_tail_bytes'], len(b'{"event":'))
        self.assertEqual(result['batches'][0]['unmatched_claims'], [batch['measurements'][0]['measurement_id']])
        unclosed = make_batch('unclosed', [('continue', 0, [])])
        alter_journal(unclosed, lambda rows: rows.pop())
        self.assertEqual(self.replay(unclosed)['unresolved_events'], 1)

    def test_third_failure_requires_quarantine_before_replacement_claim(self):
        batch = make_batch('threshold', [('continue', 0, [
            ('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1),
            ('claim', 2), ('failure', 2), ('claim', 3), ('success', 3)])])
        with self.assertRaisesRegex(ValueError, 'native_required_quarantine_missing'):
            self.replay(batch)
        # A well-formed finish cannot silently certify the missing quarantine either.
        stopped = make_batch('stopped', [('continue', 0, [
            ('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1),
            ('claim', 2), ('failure', 2)])])
        with self.assertRaisesRegex(ValueError, 'native_required_quarantine_missing'):
            self.replay(stopped)

    def test_already_active_success_cannot_hide_missing_required_quarantine(self):
        batch = make_batch('active', [('continue', 0, [
            ('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1),
            ('claim', 2), ('claim', 3), ('failure', 2), ('success', 3)])])
        with self.assertRaisesRegex(ValueError, 'native_required_quarantine_missing'):
            self.replay(batch)

    def test_prefix_ending_at_third_failure_retains_blocker_without_inventing_event(self):
        batch = make_batch('prefix', [('continue', 0, [
            ('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1),
            ('claim', 2), ('failure', 2)])])
        alter_journal(batch, lambda rows: rows.pop())
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 3)
        self.assertEqual(result['nonzero_failures'], {'model:synthetic': 3})
        self.assertEqual(result['unresolved_events'], 2)  # Unclosed controller and absent quarantine.
        self.assertEqual(result['quarantines'], {})
        self.assertNotIn('route_quarantined', result['event_counts'])
        self.assertEqual(result['batches'][0]['missing_required_quarantine'],
                         batch['measurements'][2]['measurement_id'])
        # The inherited seed reaches the same threshold, without inventing two failures.
        seeded = make_batch('seeded', [('continue', 2, [('claim', 0), ('failure', 0)])])
        alter_journal(seeded, lambda rows: rows.pop())
        self.assertEqual(self.replay(seeded)['lifetime_route_failures'], 1)
        self.assertEqual(self.replay(seeded)['nonzero_failures'], {'model:synthetic': 3})

    def test_threshold_quarantine_stays_sticky_after_already_active_success(self):
        batch = make_batch('sticky', [('continue', 0, [
            ('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1),
            ('claim', 2), ('claim', 3), ('failure', 2), ('quarantine', 2), ('success', 3)])])
        alter_journal(batch, lambda rows: next(r for r in rows if r['event'] == 'route_quarantined')
                      .update(reason='three_consecutive_infrastructure_failures'))
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 3)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual(result['unresolved_events'], 0)
        self.assertEqual(set(result['quarantines']), {'model:synthetic'})
        self.assertEqual(result['quarantine_evidence'][0]['measurement_id'], batch['measurements'][2]['measurement_id'])

    def test_quarantine_requires_immediately_preceding_failure_and_known_reason(self):
        cases = [
            [('claim', 0), ('success', 0), ('quarantine', 0)],
            [('claim', 0), ('failure', 0), ('claim', 1), ('quarantine', 0), ('success', 1)],
            [('claim', 0), ('failure', 0), ('claim', 1), ('failure', 1), ('quarantine', 0)],
        ]
        for actions in cases:
            with self.subTest(actions=actions):
                with self.assertRaisesRegex(ValueError, 'native_quarantine_link_changed'):
                    self.replay(make_batch('link', [('continue', 0, actions)]))
        for reason in ('unknown_reason', '', 'three_consecutive_infrastructure_failures'):
            with self.subTest(reason=reason):
                batch = make_batch('reason', [('continue', 0, [('claim', 0), ('failure', 0), ('quarantine', 0)])])
                alter_journal(batch, lambda rows: rows[3].update(reason=reason))
                with self.assertRaisesRegex(ValueError, 'native_quarantine_reason_changed'):
                    self.replay(batch)

    def test_seed_cannot_drop_or_invent_inheritance(self):
        first = make_batch('a', [('continue', 0, [('claim', 0), ('failure', 0)])])
        for value in (0, 2, True, 1.0, 3):
            with self.subTest(value=value):
                last = make_batch('b', [('continue', 0, [])], residual=1)
                alter_journal(last, lambda rows: rows[0].update(inherited_failures=value))
                with self.assertRaisesRegex(ValueError, 'native_inherited_failure_changed'):
                    self.replay(first, last)

    def test_invalid_terminal_claim_order_status_and_counter_refuse(self):
        base = make_batch('a', [('continue', 0, [('claim', 0), ('success', 0)])])
        changes = [
            lambda rows: rows[1].update(input_sha256='0' * 64),
            lambda rows: rows[1].update(measurement_id='point8_test/model_a/a_1/early/question'),
            lambda rows: rows[2].update(technical_status='failure'),
            lambda rows: rows[2].update(consecutive_failures=True),
            lambda rows: rows[2].update(consecutive_failures=1),
            lambda rows: rows[2]['final'].update(path='/elsewhere/FINAL.json'),
            lambda rows: rows.insert(3, copy.deepcopy(rows[2])),
            lambda rows: rows.pop(1),
        ]
        for change in changes:
            with self.subTest(change=change):
                batch = copy.deepcopy(base); alter_journal(batch, change)
                with self.assertRaises((ValueError, KeyError)):
                    self.replay(batch)

    def test_repeated_continuation_preserves_seed_without_double_counting(self):
        batch = make_batch('resume', [
            ('continue', 0, [('claim', 0), ('failure', 0)]),
            ('continue', 0, [('claim', 1), ('success', 1)]),
        ])
        result = self.replay(batch)
        self.assertEqual(result['lifetime_route_failures'], 1)
        self.assertEqual(result['nonzero_failures'], {})
        self.assertEqual(result['unresolved_events'], 0)
        alter_journal(batch, lambda rows: rows[4].update(inherited_failures=0))
        with self.assertRaisesRegex(ValueError, 'native_inherited_failure_changed'):
            self.replay(batch)

    def test_resealed_identity_scope_and_lexical_path_aliases_refuse(self):
        batch = make_batch('identity', [])
        identity = json.loads(base64.b64decode(batch['identity']['base64']))
        identity['model'] = 'other_model'
        batch['identity'] = embedded(batch['identity']['pin']['path'], raw_bundle(identity))
        with self.assertRaisesRegex(ValueError, 'native_identity_binding_changed'):
            self.replay(batch)
        batch = make_batch('path', [])
        batch['output_root'] = '//batches/path'
        with self.assertRaisesRegex(ValueError, 'noncanonical_history_path'):
            self.replay(batch)

    def test_pilot_counts_must_not_use_full_ready_denominator(self):
        batch = make_batch('a', [('pilot', 0, [])])
        alter_journal(batch, lambda rows: rows[0].update(pending=4))
        with self.assertRaisesRegex(ValueError, 'native_start_counts_changed'):
            self.replay(batch)

    def test_declared_pins_and_external_requirements_are_preserved(self):
        base = make_batch('a', [('continue', 0, [])])
        for key in ('identity', 'journal'):
            with self.subTest(key=key):
                bad = copy.deepcopy(base); bad[key]['pin']['sha256'] = '0' * 64
                with self.assertRaisesRegex(ValueError, 'embedded_history_pin_changed'):
                    self.replay(bad)
        bad = copy.deepcopy(base); del bad['evidence']['retirement']
        with self.assertRaisesRegex(ValueError, 'native_external_evidence_required'):
            self.replay(bad)
        with mock.patch('builtins.open', side_effect=AssertionError('pure replay opened a file')):
            result = self.replay(base)
        self.assertEqual(result['batches'][0]['external_evidence'], base['evidence'])
        self.assertFalse(result['physical_custody_certified'])

    def test_duplicate_or_overlapping_physical_batches_refuse(self):
        a = make_batch('a', []); b = make_batch('b', [])
        with self.assertRaisesRegex(ValueError, 'overlapping_physical_batches'):
            self.replay(a, a)
        b['output_root'] = '/batches/a/child'
        with self.assertRaisesRegex(ValueError, 'overlapping_physical_batches'):
            self.replay(a, b)
        b = make_batch('b', []); b['measurements'][0] = copy.deepcopy(a['measurements'][0])
        with self.assertRaisesRegex(ValueError, 'overlapping_or_invalid_measurement'):
            self.replay(a, b)

    def test_chain_extension_requires_exact_prefix_and_binding(self):
        a, b, c = [make_batch(name, []) for name in 'abc']
        previous = seal_batches([a, b]); current = seal_batches([a, b, c])
        result = history.validate_extension(raw_bundle(previous), raw_bundle(current))
        self.assertEqual(len(result['batches']), 3)
        changes = [
            lambda value: value['batches'].pop(0),
            lambda value: value['batches'].reverse(),
            lambda value: value['batches'][0]['evidence']['export'].update(sha256='1' * 64),
            lambda value: value.update(model_route='model:other'),
            lambda value: value['config'].update(sha256='2' * 64),
        ]
        for change in changes:
            with self.subTest(change=change):
                changed = copy.deepcopy(current); change(changed)
                with self.assertRaisesRegex(ValueError, 'native_chain_'):
                    history.validate_extension(raw_bundle(previous), raw_bundle(changed))
        changed = copy.deepcopy(current); changed['batches'][2]['previous_batch_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'native_batch_ancestry_changed'):
            history.replay_native_history(raw_bundle(changed))

    def test_unknown_or_malformed_native_envelopes_never_fall_through(self):
        good = seal_batches([make_batch('a', [])])
        wrong = copy.deepcopy(good); wrong['schema'] = 'causes.supplement_history_bundle.v2'
        cases = [raw_bundle(wrong), raw_bundle(good)[:-2], b'{"schema":',
                 b'{"schema":"causes.supplement_history_bundle.v1", "schema":"other"}\n',
                 b'{"untyped":[]}\n']
        for raw in cases:
            with self.subTest(raw=raw[:50]):
                with self.assertRaises((ValueError, KeyError)):
                    history.replay_history(raw)
        self.assertEqual(history.replay_history(raw_bundle(good)), history.replay_native_history(raw_bundle(good)))

    def test_dispatcher_replay_is_unchanged_including_partial_tail(self):
        raw = b''.join(raw_bundle(event) for event in [
            {'event': 'claimed', 'run_id': 'remote_job', 'job_sha256': 'hash'},
            {'event': 'route_failure', 'route': 'model:remote', 'consecutive_failures': 2},
            {'event': 'route_success', 'routes': ['model:remote']},
            {'event': 'unresolved_remote_attempt'},
        ])
        for value in (b'', raw, raw + b'{"event":', raw_bundle({'event': 'controller_started'})):
            self.assertEqual(history.replay_history(value), monitor.replay_history(value))

    def test_launcher_uses_maximum_native_and_remote_residual_and_exact_initial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = raw_bundle(seal_batches([make_batch('a', [('continue', 1, [])])]))
            remote = raw_bundle({'event': 'route_failure', 'route': 'model:remote', 'consecutive_failures': 2})
            scopes, current = [], []
            for name, raw, route in [('native', native, 'model:synthetic'), ('remote', remote, 'model:remote')]:
                path = root / (name + '.json'); path.write_bytes(raw); ref = core.pin(path)
                state = history.replay_history(raw)
                scope = {'id': name, 'mode': 'sealed', 'identity': {'route': route}, 'baseline': ref,
                         'model_aliases': [route], 'accepted_state': {k: state[k] for k in launcher.HISTORY_FIELDS}}
                scopes.append(scope); current.append({'id': name, 'identity': scope['identity'], 'capture': ref})
            self.assertEqual(launcher.shared_histories(scopes, current, history.replay_history), 2)
            value = {'schema': 'causes.supplement_admission_observation.v1', 'observed_from': '2026-01-01T00:00:00+00:00',
                     'observed_until': '2026-01-01T00:00:00+00:00', 'identity': {'synthetic': True},
                     'checks': dict.fromkeys(launcher.CHECKS, True), 'controls': {'/PAUSE': False},
                     'resources': dict.fromkeys(('local_free_bytes', 'remote_root_free_bytes', 'remote_scratch_free_bytes'), 100 * 1024**3),
                     'evidence': [scopes[0]['baseline']], 'donors': current}
            policy = {'runtime_identity': value['identity'], 'remote_pause_paths': ['/PAUSE'], 'donors': scopes,
                      'initial_failures': 1}
            with self.assertRaisesRegex(core.IntegrityError, 'inherited_failure_baseline_changed'):
                launcher.qualify_observation(value, policy, history.replay_history, at=value['observed_until'])
            policy['initial_failures'] = 2
            self.assertEqual(launcher.qualify_observation(value, policy, history.replay_history, at=value['observed_until']), value)

    def test_live_dispatcher_growth_opt_in_and_sealed_scope_equality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = raw_bundle({'event': 'route_success', 'routes': ['model:remote']})
            current = baseline + raw_bundle({'event': 'route_failure', 'route': 'model:remote', 'consecutive_failures': 1}) + baseline
            refs = []
            for name, raw in [('old', baseline), ('new', current)]:
                path = root / name; path.write_bytes(raw); refs.append(core.pin(path))
            state = history.replay_history(baseline)
            scope = {'id': 'remote', 'mode': 'live_prefix', 'identity': {}, 'baseline': refs[0],
                     'model_aliases': ['model:remote'], 'accepted_state': {k: state[k] for k in launcher.HISTORY_FIELDS}}
            observed = [{'id': 'remote', 'identity': {}, 'capture': refs[1]}]
            with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
                launcher.shared_histories([scope], observed, history.replay_history)
            scope['history_progression'] = 'append_only_lifetime_v1'
            self.assertEqual(launcher.shared_histories([scope], observed, history.replay_history), 0)
            scope['mode'] = 'sealed'
            with self.assertRaisesRegex(core.IntegrityError, 'sealed_donor_changed'):
                launcher.shared_histories([scope], observed, history.replay_history)


if __name__ == '__main__':
    unittest.main()
