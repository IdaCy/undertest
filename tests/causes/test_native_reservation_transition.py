"""Exact native JOBS augmentation, using the real allocator and census composer."""
import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from causes import campaign_admission as a
from causes import campaign_census as c
from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_corpus_continuation import fixture


class NativeReservationTransitionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sequence = 0

    def save(self, value, label='value'):
        self.sequence += 1
        path = self.root / (str(self.sequence) + '-' + label + '.json')
        path.write_bytes(h.encoded(value))
        return h.pin(path)

    def build(self, mode='finite', expanded=False):
        data = fixture()
        if expanded:
            # Two tasks with 257 eligible cells each: exactly 2 full + 512 prefix.
            for task, count in [('one', 2), ('two', 3)]:
                proto = next(j for j in data['prototypes'] if j['spec']['task_id'] == task)
                cell = next(c for c in data['catalog']['physical_cells'] if c['task_id'] == task)
                for index in range(count, 257):
                    job = copy.deepcopy(proto)
                    variant = job['variant']
                    variant.update(id='V' + str(index), request='synthetic ' + task + str(index),
                                   definition_sha256=s.digest([task, index]))
                    variant['stimulus_sha256'] = s.digest({k: variant.get(k) for k in
                                                          ('request', 'history', 'workspace', 'files', 'env')})
                    job['allocation']['run_id'] = 'bank_' + task + '_' + str(index)
                    original = copy.deepcopy(job)
                    original['allocation'].update(run_id='original_' + task + '_' + str(index),
                                                  source_bank83_run_id=job['allocation']['run_id'])
                    added = copy.deepcopy(cell)
                    added.update(stimulus_sha256=variant['stimulus_sha256'],
                                 earliest_accepted_canonical_variant_id=variant['id'])
                    added['source_aliases'][0].update(variant_id=variant['id'],
                                                     definition_sha256=variant['definition_sha256'])
                    data['prototypes'].append(job)
                    data['original_jobs'].append(original)
                    data['catalog']['physical_cells'].append(added)
        refs = {key: self.save(data[key], key) for key in ('catalog', 'prototypes', 'original_jobs', 'config')}
        base = self.root / ('synthetic-host-' + str(self.sequence))
        (base / 'runs/new/model').mkdir(parents=True)
        host = dict(host='fixture', base=str(base), roots=['runs/new/model'], top_jobs=[], control_directories=[],
                    limits=dict(max_file_bytes=10000, max_total_bytes=100000, max_files=20,
                                max_directory_entries=20, max_allocations=2000))
        capture = c.collect_host(host)
        capture['collector_source'] = h.pin(c.__file__)
        captures = {'fixture': dict(value=capture, pin=self.save(capture, 'capture'))}
        config = dict(schema=c.CONFIG_SCHEMA, collector_sha256=h.pin(c.__file__)['sha256'], hosts=[host],
                      frozen_inputs=[dict(pin=refs[key], selector=[], kind='jobs', expected_count=len(data[key]))
                                     for key in ('prototypes', 'original_jobs')])
        frozen = [dict(pin=refs[key], value=data[key]) for key in ('prototypes', 'original_jobs')]
        old = c.compose_reservation(config, captures, frozen)
        old_ref = self.save(old, 'baseline')
        finite, prefix = mode == 'finite', mode == 'prefix'
        control = dict(schema='causes.corpus_continuation_control.v1' if prefix else 'causes.corpus_continuation_control.v2',
                       **refs, reservation=old_ref, repetition_start=1,
                       **{k: data[k] for k in ('family', 'model_key', 'task_host', 'provider_host')})
        if not prefix:
            control['full_selection'] = {'method': 'one_cell_per_task_rotating_v1'}
        control_ref = self.save(control, 'control')
        allocator = CorpusContinuation.from_control(control, data['prototypes'], data['config'])
        jobs = allocator.block(0)['jobs'] if finite else []
        jobs_ref = self.save(jobs, 'jobs')
        allocation = dict(schema='causes.native_corpus_allocation.v1', origin='native_successor',
                          no_fixed_study_pooling=True, target_mode='finite_qualification' if finite else 'continuous_corpus',
                          workload='prefix_only' if prefix else 'mixed', workers=8 if prefix else 4, max_full=0 if prefix else 1,
                          family=data['family'], model_key=data['model_key'], controller_host='fixture', jobs=jobs_ref,
                          config=refs['config'], extensions=self.save([]) if finite else refs['prototypes'],
                          allocation_control=control_ref, control=None if finite else control_ref,
                          selection=dict(block_index=0, run_ids=[j['allocation']['run_id'] for j in jobs]) if finite else None,
                          output_root=str(base / 'runs/new/model'))
        fresh_config = copy.deepcopy(config)
        fresh_config['frozen_inputs'].append(dict(pin=jobs_ref, selector=[], kind='jobs', expected_count=len(jobs),
                                                  allowed_locations=[['fixture', 'runs/new/model']]))
        fresh = c.compose_reservation(fresh_config, captures, frozen + [dict(pin=jobs_ref, value=jobs)])
        return old, fresh, allocation, self.save(fresh_config, 'fresh-config')

    def operator(self, old, fresh, allocation, config):
        rules, inputs = {}, {}
        foundation = dict(at=h.now(), provider={}, host_transfer={}, legacy_owners=[], predecessors=[],
                          legacy_owners_healthy=True, provider_tunnels_healthy=True, original_transfer_present=True,
                          donor_histories_clear=True, native_no_active_owned_work=True,
                          all_predecessors_absent_and_unlocked=True, donor_cuts=[], evidence_pins=[])
        for key, value in [('precreation_reservation', old), ('fresh_reservation', fresh), ('foundation', foundation)]:
            evidence = h.checked(allocation['allocation_control'])['reservation'] if key == 'precreation_reservation' else self.save(value)
            flag = 'accepted_' + key
            review = {flag: True, 'evidence': evidence}
            if key == 'fresh_reservation':
                review['census_config'] = config
            inputs[key] = dict(evidence=evidence, review=self.save(review))
            rules[key] = dict(flag=flag, binding_key='evidence')
        operator = a.NativeAdmission.__new__(a.NativeAdmission)
        operator.policy = dict(allocation=self.save(allocation), acceptance_rules=rules, census_hosts=['fixture'],
                               census_configs=dict(before_start=config, waiting_release=config), provider={}, host_transfer={},
                               legacy_owners=[], predecessors=[], history={'donor_scopes': []}, donor_histories=[],
                               reservation_transition='exact_own_allocation_v1')
        operator.inputs = inputs
        return operator, foundation

    def check(self, old, fresh, allocation):
        return a.native_reservation_augmentation(old, fresh, allocation)

    def test_finite_census_contains_all_unclaimed_jobs_and_regenerates_exactly(self):
        old, fresh, allocation, config = self.build(expanded=True)
        jobs = s.native_allocation(allocation)[0]
        self.assertEqual((len(jobs), sum(j['allocation']['continue_to_end'] for j in jobs)), (514, 2))
        self.assertEqual(len(fresh['run_ids']) - len(old['run_ids']), 514)
        before = copy.deepcopy((old, fresh, allocation))
        self.assertTrue(self.check(old, fresh, allocation))
        operator, foundation = self.operator(old, fresh, allocation, config)
        self.assertEqual(operator.evidence(operator.start_action), foundation)
        self.assertEqual((old, fresh, allocation), before)

    def test_mixed_and_prefix_empty_queues_still_require_exact_jobs_pin(self):
        for mode in ('mixed', 'prefix'):
            with self.subTest(mode=mode):
                old, fresh, allocation, config = self.build(mode)
                self.assertEqual(old['allocation_identities'], fresh['allocation_identities'])
                self.assertEqual(old['allocation_seeds'], fresh['allocation_seeds'])
                self.assertEqual(fresh['frozen_input_pins'], old['frozen_input_pins'] + [allocation['jobs']])
                self.assertTrue(self.check(old, fresh, allocation))
                operator, foundation = self.operator(old, fresh, allocation, config)
                self.assertEqual(operator.evidence(operator.start_action), foundation)
                with self.assertRaisesRegex(ValueError, 'frozen_census_scope_changed'):
                    self.check(old, old, allocation)

    def test_absent_opt_in_preserves_strict_behavior_and_null_or_unknown_refuses(self):
        for mode in ('finite', 'mixed', 'prefix'):
            old, fresh, allocation, config = self.build(mode)
            operator, _ = self.operator(old, fresh, allocation, config)
            del operator.policy['reservation_transition']
            wanted = 'reservation_changed_requires_new_review' if mode == 'finite' else 'frozen_census_scope_changed'
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, wanted):
                operator.evidence(operator.start_action)
            for invalid in (None, '', 'unknown', True, {}):
                operator.policy['reservation_transition'] = invalid
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'transition_unknown'):
                    operator.evidence(operator.start_action)
            self.assertTrue(a.reservation_unchanged(old, old))

    def test_missing_extra_duplicate_and_modified_current_identities_refuse(self):
        old, fresh, allocation, _ = self.build()
        own = next(row for row in fresh['allocation_identities'] if row['run_id'] not in old['run_ids'])
        foreign = dict(own, run_id='foreign_unrelated')
        variants = []
        for row in (own, old['allocation_identities'][0]):
            changed = copy.deepcopy(fresh)
            changed['allocation_identities'] = [r for r in changed['allocation_identities'] if r['run_id'] != row['run_id']]
            changed['run_ids'].remove(row['run_id'])
            variants.append(changed)
        for row in (foreign, own):
            changed = copy.deepcopy(fresh)
            changed['allocation_identities'].append(copy.deepcopy(row))
            if row is foreign:
                changed['run_ids'] = sorted(changed['run_ids'] + [row['run_id']])
            variants.append(changed)
        for wanted in (own['run_id'], old['run_ids'][0]):
            changed = copy.deepcopy(fresh)
            next(r for r in changed['allocation_identities'] if r['run_id'] == wanted)['seed'] = 2
            variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed['run_ids']), self.assertRaises(ValueError):
                self.check(old, changed, allocation)

    def test_seed_union_cannot_add_remove_or_coerce_boolean(self):
        old, fresh, allocation, _ = self.build()
        for seeds in (fresh['allocation_seeds'] + [2147483646], fresh['allocation_seeds'][1:],
                      [True, *fresh['allocation_seeds'][1:]]):
            changed = copy.deepcopy(fresh); changed['allocation_seeds'] = seeds
            with self.subTest(seeds=seeds), self.assertRaisesRegex(ValueError, 'reservation_seed_union_changed'):
                self.check(old, changed, allocation)
        changed = copy.deepcopy(fresh); changed['allocation_identities'][0]['seed'] = True
        with self.assertRaisesRegex(ValueError, 'typed_allocation'):
            self.check(old, changed, allocation)

    def test_exact_one_frozen_pin_in_original_order_not_an_alias(self):
        old, fresh, allocation, _ = self.build()
        alternate_jobs = self.save(h.checked(allocation['jobs']), 'equal-byte-other-path')
        for pins in (fresh['frozen_input_pins'][:-1], fresh['frozen_input_pins'] + [allocation['jobs']],
                     list(reversed(fresh['frozen_input_pins'])), old['frozen_input_pins'] + [alternate_jobs],
                     fresh['frozen_input_pins'] + [self.save({'foreign': True})]):
            changed = copy.deepcopy(fresh); changed['frozen_input_pins'] = pins
            with self.subTest(pins=pins), self.assertRaisesRegex(ValueError, 'frozen_census_scope_changed'):
                self.check(old, changed, allocation)

    def test_unrelated_persisted_block_change_refuses_even_same_union(self):
        old, fresh, allocation, _ = self.build()
        changed = copy.deepcopy(fresh)
        changed['persisted_full_blocks'] = [{'host': 'fixture', 'file': {'path': '/foreign'}, 'allocation_count': 0}]
        with self.assertRaisesRegex(ValueError, 'native_reservation_blocks_changed'):
            self.check(old, changed, allocation)

    def test_freshness_and_original_host_scope_remain_required(self):
        old, fresh, allocation, config = self.build()
        for cuts in ([], fresh['host_cuts'] * 2, [dict(fresh['host_cuts'][0], host='other')]):
            changed = copy.deepcopy(fresh); changed['host_cuts'] = cuts
            with self.subTest(cuts=cuts), self.assertRaisesRegex(ValueError, 'native_reservation_hosts_changed'):
                self.check(old, changed, allocation)
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=181)).isoformat()
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10)).isoformat()
        for time in (stale, future, '2026-01-01T00:00:00'):
            changed = copy.deepcopy(fresh); changed['host_cuts'][0]['started_at'] = time
            with self.subTest(time=time), self.assertRaisesRegex(ValueError, 'admission_observation_stale'):
                self.check(old, changed, allocation)
        operator, _ = self.operator(old, fresh, allocation, config)
        operator.policy['census_hosts'] = ['other']
        with self.assertRaisesRegex(ValueError, 'native_reservation_hosts_changed'):
            operator.evidence(operator.start_action)

    def test_current_census_config_review_still_binds_action_scope(self):
        old, fresh, allocation, config = self.build()
        operator, _ = self.operator(old, fresh, allocation, config)
        operator.policy['census_configs']['before_start'] = self.save({'unreviewed': True})
        with self.assertRaisesRegex(ValueError, 'native_reservation_roots_changed'):
            operator.evidence(operator.start_action)

    def test_complete_jobs_regeneration_rejects_mode_order_and_nonidentity_changes(self):
        old, fresh, allocation, _ = self.build()
        jobs = h.checked(allocation['jobs'])
        for change in ('mode', 'order', 'spec'):
            changed_jobs = copy.deepcopy(jobs)
            if change == 'mode': changed_jobs[0]['allocation']['continue_to_end'] = not changed_jobs[0]['allocation']['continue_to_end']
            elif change == 'order': changed_jobs.reverse()
            else: changed_jobs[0]['spec']['image'] = 'changed'
            changed = copy.deepcopy(allocation); changed['jobs'] = self.save(changed_jobs)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'native_finite_selection_changed'):
                self.check(old, fresh, changed)

    def test_control_cannot_be_rebased_and_original_typed_union_cannot_be_malformed(self):
        old, fresh, allocation, _ = self.build()
        control = h.checked(allocation['allocation_control'])
        control['reservation'] = self.save(fresh)
        changed = copy.deepcopy(allocation); changed['allocation_control'] = self.save(control)
        with self.assertRaisesRegex(ValueError, 'native_generation_reservation_changed'):
            self.check(old, fresh, changed)
        with self.assertRaisesRegex(ValueError, 'continuation_namespace_already_reserved'):
            self.check(fresh, fresh, changed)
        for key, value in [('run_ids', old['run_ids'][1:]), ('allocation_seeds', [True, 2, 3, 4]),
                           ('allocation_identities', old['allocation_identities'] * 2)]:
            broken = copy.deepcopy(old); broken[key] = value
            control = h.checked(allocation['allocation_control']); control['reservation'] = self.save(broken)
            changed = copy.deepcopy(allocation); changed['allocation_control'] = self.save(control)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(broken, fresh, changed)


if __name__ == '__main__':
    unittest.main()
