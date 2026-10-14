"""Synthetic future plans; no executor, model, or genuine closure is created."""
import copy
import hashlib
import json
import unittest

from causes import campaign_supplement_source_lineage as lineage


class Saved:
    def __init__(self):
        self.data = {}
        self.serial = 0

    def put(self, value):
        self.serial += 1
        path = '/fixture/record_' + str(self.serial)
        data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
        self.data[path] = data
        return dict(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def read(self, ref):
        return self.data[ref['path']]

    def value(self, ref):
        return json.loads(self.read(ref))

    def change(self, ref, **fields):
        return self.put(dict(self.value(ref), **fields))

    def closed(self, plan):
        actual = self.put(dict(plan=plan, native_invocations=[dict(plan=plan)],
                               complete_ids=['old/failed', 'old/success'],
                               consecutive_failures=1, retained_failures=['old/failed']))
        review = self.put(dict(accepted_finite_supplement_closed_custody=True,
                               actual=actual, findings=[]))
        return dict(actual=actual, review=review)


def fixture():
    s = Saved()
    old_native = s.put(b'old native queue'); old_runtime = s.put(b'old runtime helper')
    accepted_helper = s.put(b'accepted public helper')
    native = s.put(b'healthy native queue'); actor = s.put(b'healthy completed actor')
    first_driver = s.put(b'first driver'); next_driver = s.put(b'continuation driver')
    binder = s.put(b'first binder'); next_binder = s.put(b'continuation binder')
    parents = {'causes.campaign_supplement': old_native,
               'causes.campaign_supplement_completed_actor': accepted_helper}
    selected = {'causes.campaign_supplement': native,
                'causes.campaign_supplement_completed_actor': actor}
    derivation = s.put(dict(only_early_gate_changed=True, default_opt_in=False,
        proofs=[dict(module=k, source=selected[k], parent=parents[k]) for k in selected]))
    preparation = s.put(dict(sources=selected, parent_sources=parents, source_derivation=derivation))
    peer = s.put(dict(accepted_healthy_harvest_continuation_source=True,
        accepted_completed_actor_healthy_harvest_source=True, sources=selected,
        parent_sources=parents, preparation=preparation, source_derivation=derivation, findings=[]))
    adoption = s.put(dict(schema='causes.healthy_harvest_source_root_adoption.v1',
        accepted_source_for_prospective_migration=True, sources=selected,
        parent_sources=parents, source_preparation=preparation, independent_review=peer, findings=[]))
    before = s.put(dict(sources=dict(native_queue=old_native, completed_actor=old_runtime)))
    before_closed = s.closed(before)
    origin = s.put(dict(schema=lineage.MIGRATION, kind=lineage.KIND,
        predecessor_plan=before, previous_source=old_native,
        previous_runtime_completed_actor_source=old_runtime, accepted_public_dependency=accepted_helper,
        source=native, completed_actor_source=actor, driver_source=first_driver, binder_source=binder,
        source_review=peer, root_source_adoption=adoption, source_preparation=preparation,
        source_derivation=derivation))
    migrated = s.put(dict(sources=dict(native_queue=native, completed_actor=actor, driver=first_driver),
        retain_admission_after_healthy_harvest=True, native_source_migration=origin,
        predecessor_custody=before_closed))
    predecessor = s.closed(migrated)
    sources = dict(native_queue=native, completed_actor=actor, driver=next_driver)
    step = s.put(dict(schema=lineage.CONTINUATION, kind=lineage.KIND,
        predecessor_plan=migrated, previous_migration=origin, original_migration=origin,
        previous_source=native, source=native,
        previous_runtime_completed_actor_source=actor, completed_actor_source=actor,
        driver_source=next_driver, binder_source=next_binder))
    return s, dict(reference=step, predecessor=predecessor, sources=sources, read=s.read), origin


class SourceLineageTests(unittest.TestCase):
    def check(self, kw):
        return lineage.validate_continuation(**kw)

    def refuse(self, kw, text=None):
        with self.assertRaises(lineage.SourceLineageError) as caught:
            self.check(kw)
        if text:
            self.assertIn(text, str(caught.exception))

    def test_migrated_then_same_source_keeps_first_migration_and_failures(self):
        s, kw, origin = fixture(); before = copy.deepcopy(s.data)
        result = self.check(kw)
        self.assertEqual(result['original_migration'], origin)
        self.assertEqual(result['continuation_count'], 1)
        self.assertEqual(result['kind'], 'same_source_continuation')
        self.assertEqual(s.data, before)
        self.assertEqual(s.value(kw['predecessor']['actual'])['consecutive_failures'], 1)
        self.assertEqual({p['path'] for p in result['required_pins']}, set(s.data))

    def test_second_continuation_follows_exact_first_continuation(self):
        s, kw, origin = fixture()
        previous = s.put(dict(sources=kw['sources'], retain_admission_after_healthy_harvest=True,
                             native_source_migration=kw['reference'], predecessor_custody=kw['predecessor']))
        step = s.value(kw['reference'])
        step.update(predecessor_plan=previous, previous_migration=kw['reference'])
        kw.update(reference=s.put(step), predecessor=s.closed(previous))
        self.assertEqual(self.check(kw)['continuation_count'], 2)
        self.assertEqual(self.check(kw)['original_migration'], origin)

    def test_initial_migration_cannot_be_replayed_as_continuation(self):
        _, kw, origin = fixture(); kw['reference'] = origin
        self.refuse(kw, 'continuation_required')

    def test_changed_queue_even_with_new_correct_pin_is_refused(self):
        s, kw, _ = fixture(); new = s.put(b'changed queue')
        kw['sources']['native_queue'] = new
        kw['reference'] = s.change(kw['reference'], source=new)
        self.refuse(kw, 'sources_changed')

    def test_changed_actor_even_with_new_correct_pin_is_refused(self):
        s, kw, _ = fixture(); new = s.put(b'changed helper')
        kw['sources']['completed_actor'] = new
        kw['reference'] = s.change(kw['reference'], completed_actor_source=new)
        self.refuse(kw, 'sources_changed')

    def test_equal_bytes_at_another_path_are_not_same_pin(self):
        s, kw, _ = fixture(); new = s.put(s.read(kw['sources']['native_queue']))
        kw['sources']['native_queue'] = new
        kw['reference'] = s.change(kw['reference'], source=new)
        self.refuse(kw, 'sources_changed')

    def test_previous_lineage_cannot_be_substituted(self):
        s, kw, origin = fixture(); duplicate = s.put(s.read(origin))
        kw['reference'] = s.change(kw['reference'], previous_migration=duplicate)
        self.refuse(kw, 'previous_record')

    def test_original_migration_pin_cannot_be_substituted(self):
        s, kw, origin = fixture(); duplicate = s.put(s.read(origin))
        kw['reference'] = s.change(kw['reference'], original_migration=duplicate)
        self.refuse(kw, 'original_changed')

    def test_false_or_numeric_prior_opt_in_refused(self):
        for flag in (False, 1, None):
            with self.subTest(flag=flag):
                s, kw, _ = fixture(); step = s.value(kw['reference'])
                prior = s.change(step['predecessor_plan'], retain_admission_after_healthy_harvest=flag)
                kw.update(reference=s.put(dict(step, predecessor_plan=prior)), predecessor=s.closed(prior))
                self.refuse(kw, 'previous_record')

    def test_derived_predecessor_cannot_use_original_transfer_chain(self):
        s, kw, _ = fixture(); step = s.value(kw['reference'])
        prior = s.change(step['predecessor_plan'], derived_bank={})
        kw.update(reference=s.put(dict(step, predecessor_plan=prior)), predecessor=s.closed(prior))
        self.refuse(kw, 'previous_record')

    def test_closed_peer_must_join_exact_actual(self):
        s, kw, _ = fixture()
        kw['predecessor']['review'] = s.change(kw['predecessor']['review'], actual=kw['reference'])
        self.refuse(kw, 'closed_peer')

    def test_closed_peer_refusal_is_not_ignored(self):
        s, kw, _ = fixture()
        kw['predecessor']['review'] = s.change(kw['predecessor']['review'], findings=['failure'])
        self.refuse(kw, 'closed_peer')

    def test_current_plan_must_be_last_native_invocation(self):
        s, kw, _ = fixture(); actual = s.change(kw['predecessor']['actual'], native_invocations=[])
        kw['predecessor'] = dict(actual=actual, review=s.put(dict(
            accepted_finite_supplement_closed_custody=True, actual=actual, findings=[])))
        self.refuse(kw, 'immediate_plan')

    def test_changed_original_source_bytes_are_rehashed(self):
        s, kw, origin = fixture(); original = s.value(origin)
        s.data[original['previous_source']['path']] = b'tampered'
        self.refuse(kw, 'pin_bytes')

    def test_original_acceptance_flags_are_required(self):
        s, kw, origin = fixture(); original = s.value(origin)
        peer = s.value(original['source_review']); peer['accepted_healthy_harvest_continuation_source'] = False
        changed_peer = s.put(peer); original['source_review'] = changed_peer
        original['root_source_adoption'] = s.change(original['root_source_adoption'], independent_review=changed_peer)
        new_origin = s.put(original); step = s.value(kw['reference'])
        prior = s.change(step['predecessor_plan'], native_source_migration=new_origin)
        kw.update(reference=s.put(dict(step, predecessor_plan=prior, previous_migration=new_origin,
                                     original_migration=new_origin)), predecessor=s.closed(prior))
        self.refuse(kw, 'original_acceptance')

    def test_wrong_driver_refused(self):
        s, kw, _ = fixture()
        kw['reference'] = s.change(kw['reference'], driver_source=s.put(b'another driver'))
        self.refuse(kw, 'endpoint')

    def test_depth_bound_refuses_longer_chain(self):
        s, kw, _ = fixture()
        prior = s.put(dict(sources=kw['sources'], retain_admission_after_healthy_harvest=True,
                          native_source_migration=kw['reference'], predecessor_custody=kw['predecessor']))
        kw.update(reference=s.change(kw['reference'], predecessor_plan=prior,
                                    previous_migration=kw['reference']), predecessor=s.closed(prior), max_links=1)
        self.refuse(kw, 'depth_exceeded')

    def test_pin_bound_checked_before_reader(self):
        _, kw, _ = fixture(); calls = []
        kw['reference'] = dict(kw['reference'], bytes=8*1024**2+1)
        kw['read'] = lambda ref: calls.append(ref)
        self.refuse(kw, 'pin_shape_or_bound'); self.assertEqual(calls, [])

    def test_duplicate_json_key_refused(self):
        s, kw, _ = fixture(); kw['reference'] = s.put(b'{"schema":1,"schema":2}')
        self.refuse(kw, 'duplicate_json_key')

    def test_invalid_depth_and_pin_types(self):
        for depth in (True, 0, 65):
            _, kw, _ = fixture(); kw['max_links'] = depth; self.refuse(kw, 'depth_limit')
        _, kw, _ = fixture(); kw['reference'] = dict(kw['reference'], bytes=True)
        self.refuse(kw, 'pin_shape_or_bound')


if __name__ == '__main__':
    unittest.main()
