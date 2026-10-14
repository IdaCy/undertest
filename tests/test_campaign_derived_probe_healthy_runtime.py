"""Synthetic source/peer boundaries; no historical data or requests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_derived_probe_runtime as runtime
from causes import campaign_supplement as core
from test_campaign_derived_probe_runtime import Fixture


class HealthyFixture(Fixture):
    def __init__(self, root, changed_helper=False):
        super().__init__(root, changed=True)
        self.intermediate = self.new
        self.early = self.review
        body = Path(self.operational.__file__).read_bytes()
        body += b'\n# Synthetic second independently reviewed queue revision.\n'
        if changed_helper:
            body = body.replace(b'80 * 1024**3', b'81 * 1024**3')
        self.operational = self.module('healthy', body)
        self.new = core.pin(self.operational.__file__)
        actor = core.pin(Path(core.__file__).with_name('campaign_supplement_completed_actor.py'))
        selected = {'causes.campaign_supplement': self.new,
                    'causes.campaign_supplement_completed_actor': actor}
        parents = dict(selected, **{'causes.campaign_supplement': self.intermediate})
        derivation = dict(only_early_gate_changed=True, default_opt_in=False,
            proofs=[dict(module=k, source=v, parent=parents[k]) for k,v in selected.items()])
        derivation_ref = self.save('healthy_derivation', derivation)
        preparation = dict(sources=selected, parent_sources=parents, source_derivation=derivation_ref)
        preparation_ref = self.save('healthy_preparation', preparation)
        peer = dict(accepted_healthy_harvest_continuation_source=True,
            accepted_completed_actor_healthy_harvest_source=True, findings=[], sources=selected,
            parent_sources=parents, preparation=preparation_ref, source_derivation=derivation_ref)
        peer_ref = self.save('healthy_review', peer)
        adoption = dict(schema='causes.healthy_harvest_source_root_adoption.v1',
            accepted_source_for_prospective_migration=True, independent_review=peer_ref,
            sources=selected, parent_sources=parents, findings=[], source_preparation=preparation_ref)
        self.descriptor = dict(schema='causes.derived_probe_healthy_queue_compatibility.v1',
            scientific_source=self.old, intermediate_source=self.intermediate,
            operational_source=self.new, completed_actor_source=actor,
            early_review=self.early, healthy_review=peer_ref, healthy_preparation=preparation_ref,
            healthy_adoption=self.save('healthy_adoption', adoption), healthy_derivation=derivation_ref)
        self.review = self.save('compatibility', self.descriptor)
        self.serial = 0

    def change(self, key, mutate):
        self.serial += 1
        value = json.loads(core.pinned(self.descriptor[key]))
        mutate(value)
        self.descriptor[key] = self.save('changed_'+str(self.serial), value)
        self.review = self.save('descriptor_'+str(self.serial), self.descriptor)

    def scope_change(self, **changes):
        self.descriptor.update(changes)
        self.review = self.save('changed_scope', self.descriptor)


class HealthyRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_exact_two_step_chain_returns_complete_immutable_roster(self):
        f = HealthyFixture(self.root)
        before = copy.deepcopy(f.protocol)
        refs = runtime.compatible_queue(f.scientific,f.operational,f.old,f.new,review=f.review)
        self.assertEqual(len(refs),len({r['path'] for r in refs}))
        for ref in [f.review,f.old,f.new,f.intermediate,*[
                value for value in f.descriptor.values() if isinstance(value,dict)]]:
            self.assertIn(ref,refs)
        adapter=f.build()
        self.assertEqual(adapter.prepare(f.job)[1]['id'],'evaluation_presence')
        self.assertEqual(adapter.protocol,before)
        self.assertNotIn('retain_admission_after_healthy_harvest',adapter.protocol)

    def test_unaccepted_healthy_peer_refuses(self):
        f=HealthyFixture(self.root)
        f.change('healthy_review',lambda x:x.update(accepted_healthy_harvest_continuation_source=False))
        with self.assertRaises(f.operational.IntegrityError):f.build()

    def test_unaccepted_root_source_adoption_refuses(self):
        f=HealthyFixture(self.root)
        f.change('healthy_adoption',lambda x:x.update(accepted_source_for_prospective_migration=False))
        with self.assertRaises(f.operational.IntegrityError):f.build()

    def test_first_transition_parent_mismatch_refuses(self):
        f=HealthyFixture(self.root)
        f.change('early_review',lambda x:x['complete_inverse'].update(parent_sha256='0'*64))
        with self.assertRaisesRegex(f.operational.IntegrityError,'first_transition'):f.build()

    def test_missing_first_transition_refuses(self):
        f=HealthyFixture(self.root);f.scope_change(early_review=None)
        with self.assertRaises(f.operational.IntegrityError):f.build()

    def test_source_endpoint_swap_refuses(self):
        f=HealthyFixture(self.root);f.scope_change(operational_source=f.intermediate)
        with self.assertRaisesRegex(f.operational.IntegrityError,'source_endpoints'):f.build()

    def test_second_peer_cannot_authorize_changed_scientific_helpers(self):
        f=HealthyFixture(self.root,changed_helper=True)
        with self.assertRaisesRegex(f.operational.IntegrityError,'request_helpers_changed'):f.build()

    def test_hidden_descriptor_fields_refuse(self):
        f=HealthyFixture(self.root);f.scope_change(allow_changed_requests=True)
        with self.assertRaisesRegex(f.operational.IntegrityError,'descriptor'):f.build()

    def test_mismatched_completed_actor_join_refuses(self):
        f=HealthyFixture(self.root);f.scope_change(completed_actor_source=f.old)
        with self.assertRaisesRegex(f.operational.IntegrityError,'second_transition'):f.build()

    def test_changed_derivation_bytes_refuse(self):
        f=HealthyFixture(self.root)
        Path(f.descriptor['healthy_derivation']['path']).write_bytes(b'{}')
        with self.assertRaises(f.operational.IntegrityError):f.build()

    def test_duplicate_derivation_module_refuses(self):
        f=HealthyFixture(self.root)
        f.change('healthy_derivation',lambda x:x['proofs'].append(x['proofs'][0]))
        with self.assertRaisesRegex(f.operational.IntegrityError,'derivation_roster'):f.build()

    def test_healthy_bridge_preserves_error_identity_and_integrity_cause(self):
        f=HealthyFixture(self.root);adapter=f.build()
        f.prepare_error=f.scientific.IntegrityError('changed')
        with self.assertRaises(f.operational.IntegrityError) as caught:adapter.prepare(f.job)
        self.assertIs(caught.exception.__cause__,f.prepare_error)
        for error in (f.scientific.AdmissionHold('expired'),RuntimeError('failed')):
            f.prepare_error=error
            with self.assertRaises(type(error))as caught:adapter.prepare(f.job)
            self.assertIs(caught.exception,error)


if __name__=='__main__':unittest.main()
