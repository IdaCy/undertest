import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import unittest


spec = importlib.util.spec_from_file_location('history_successor', Path(__file__).parents[1]
    / 'src/causes/campaign_shared_history_successor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    files = {}

    def store(name, raw):
        if not isinstance(raw, bytes):
            raw = module.encoded(raw)
        path = '/saved/' + name
        files[path] = raw
        return dict(path=path, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    state = dict(lifetime_route_failures=1, nonzero_failures={}, quarantines={},
                 unresolved_events=0, unconsumed_tail_bytes=0)
    before = b'old failure\ncurrent start\n'
    after = before + b'new failure\nnew success\n'
    baseline = store('baseline', before)
    successor = store('successor', after)
    current = store('current', b'current start\n')
    next_current = store('next-current', b'current start\nnew failure\nnew success\n')
    old = dict(initial_failures=0, identity={'model': 'same'}, required_pins=[baseline],
               donors=[dict(id='actor', mode='live_prefix', identity={'process': 42},
                            model_aliases=['model'], baseline=baseline, accepted_state=state),
                       dict(id='closed', mode='sealed', baseline=baseline, accepted_state=state)])
    new = copy.deepcopy(old)
    new['donors'][0].update(baseline=successor,
                            accepted_state=dict(state, lifetime_route_failures=2))
    suffix = [successor, next_current]
    new['required_pins'] += suffix
    transition = dict(schema=module.SCHEMA, donor_id='actor',
                      previous_policy=store('old-policy', old),
                      successor_policy=store('new-policy', new),
                      previous_current_journal=current, successor_current_journal=next_current,
                      required_pin_suffix=suffix)

    def replay(raw):
        return dict(state, lifetime_route_failures=raw.count(b'failure\n'))

    return files, store, old, new, transition, replay


def run(f, replay=None):
    files, _, _, _, transition, native = f
    return module.validate_successor(transition, read_bytes=lambda ref: files[ref['path']],
                                     replay=native if replay is None else replay)


class HistorySuccessorTests(unittest.TestCase):
    def test_exact_successor_preserves_original_inputs(self):
        f = fixture()
        original = copy.deepcopy(f[:5])
        result = run(f)
        self.assertEqual(result['before_state']['lifetime_route_failures'], 1)
        self.assertEqual(result['after_state']['lifetime_route_failures'], 2)
        self.assertEqual(result['native_replays'], 2)
        self.assertFalse(result['runtime_authorized'])
        self.assertEqual(f[:5], original)

    def test_unrelated_policy_changes_rejected(self):
        for change in ['identity', 'sealed', 'roster', 'aliases', 'mode',
                       'progression', 'initial', 'pins', 'duplicate']:
            with self.subTest(change=change):
                f = fixture()
                _, store, old, new, transition, _ = f
                if change == 'identity': new['identity']['model'] = 'different'
                if change == 'sealed': new['donors'][1]['accepted_state']['lifetime_route_failures'] = 2
                if change == 'roster': new['donors'].reverse()
                if change == 'aliases': new['donors'][0]['model_aliases'].append('another')
                if change == 'mode': new['donors'][0]['mode'] = 'sealed'
                if change == 'progression': new['donors'][0]['history_progression'] = 'append_only_lifetime_v1'
                if change == 'initial': new['initial_failures'] = False
                if change == 'pins': new['required_pins'].pop(0)
                if change == 'duplicate':
                    transition['required_pin_suffix'].append(old['required_pins'][0])
                    new['required_pins'].append(old['required_pins'][0])
                transition['successor_policy'] = store('new-policy', new)
                with self.assertRaises(module.InvalidSuccessor): run(f)

    def test_unresolved_or_invalid_native_state_rejected(self):
        for field, value in [('nonzero_failures', {'model': 1}),
                ('quarantines', {'model': True}), ('unresolved_events', 1),
                ('unconsumed_tail_bytes', 1), ('lifetime_route_failures', True)]:
            with self.subTest(field=field):
                f = fixture()
                native = f[-1]
                with self.assertRaises(module.InvalidSuccessor):
                    run(f, lambda raw: dict(native(raw), **{field: value}))

    def test_current_journal_must_supply_exact_extension(self):
        for raw in [b'edited current\nnew failure\nnew success\n',
                    b'current start\nnew success\n',
                    b'current start\nnew failure\nnew success', b'current start\n']:
            with self.subTest(raw=raw):
                f = fixture()
                _, store, _, new, transition, _ = f
                changed = store('next-current', raw)
                transition['successor_current_journal'] = changed
                transition['required_pin_suffix'][-1] = changed
                new['required_pins'][-1] = changed
                transition['successor_policy'] = store('new-policy', new)
                with self.assertRaises(module.InvalidSuccessor): run(f)

    def test_changed_artifact_rejected_even_with_permissive_reader(self):
        f = fixture()
        f[0]['/saved/baseline'] += b'changed\n'
        with self.assertRaisesRegex(module.InvalidSuccessor, 'reference_content_changed'):
            run(f)

    def test_forged_native_state_rejected(self):
        f = fixture()
        with self.assertRaisesRegex(module.InvalidSuccessor, 'native_state_not_bound'):
            run(f, lambda raw: dict(f[-1](raw), lifetime_route_failures=0))

    def test_unknown_transition_key_rejected(self):
        f = fixture()
        f[4]['skip_history'] = True
        with self.assertRaisesRegex(module.InvalidSuccessor, 'transition_schema'):
            run(f)


if __name__ == '__main__':
    unittest.main()
