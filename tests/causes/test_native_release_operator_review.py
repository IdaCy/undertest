"""Independent release binding, scope and fail-closed challenges."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from causes import campaign_release as r, campaign_handoff as h
from causes import campaign_corpus_succession as s
from tests.causes import test_native_release_operator as fixture_module


class ReleaseOperatorReviewTests(unittest.TestCase):
    def fixture(self):
        return fixture_module.ReleaseFixture(self)

    def rebound(self, fixture, operator, change):
        inputs = copy.deepcopy(operator.inputs)
        change(inputs)
        return r.ReleaseOnlyNativeAdmission(operator.policy_ref,
            fixture.save('changed-inputs.json', inputs), {})

    def test_pair_requires_exact_two_members(self):
        for change in (lambda p:p.pop('review'), lambda p:p.update(extra=True)):
            with self.subTest(change=change):
                f=self.fixture();op=f.bind()
                with self.assertRaisesRegex(ValueError, 'release_operator_pair_required'):
                    self.rebound(f,op,lambda x:change(x['release_operator']))

    def test_same_bytes_at_another_operator_path_are_not_the_loaded_source(self):
        f=self.fixture();op=f.bind();alias=f.root/'same-source.py'
        alias.write_bytes(Path(r.__file__).read_bytes())
        with self.assertRaisesRegex(ValueError, 'release_operator_source_changed'):
            self.rebound(f,op,lambda x:x['release_operator'].update(source=h.pin(alias)))

    def test_review_semantics_and_proof_are_required_even_with_valid_new_pins(self):
        changes = [lambda v:v.update(accepted_native_release_operator_sources=False),
                   lambda v:v.update(contract='wrong-contract'),
                   lambda v:v.update(base_sources={})]
        for change in changes:
            f=self.fixture();op=f.bind();review=h.checked(op.inputs['release_operator']['review'])
            change(review);ref=f.save('changed-review.json',review)
            with self.assertRaises(ValueError):
                self.rebound(f,op,lambda x:x['release_operator'].update(review=ref))
        f=self.fixture();op=f.bind();review=h.checked(op.inputs['release_operator']['review'])
        proof=h.checked(review['inverse_proof']);proof['whole_function_inverse']['lineage']=False
        review['inverse_proof']=f.save('changed-proof.json',proof)
        ref=f.save('changed-review.json',review)
        with self.assertRaisesRegex(ValueError,'release_operator_inverse_not_accepted'):
            self.rebound(f,op,lambda x:x['release_operator'].update(review=ref))

    def test_each_pinned_binding_drift_permanently_invalidates_after_restoration(self):
        for kind in ('inputs','policy','review','proof'):
            with self.subTest(kind=kind):
                f=self.fixture();op=f.bind()
                review_ref=op.inputs['release_operator']['review']
                ref={'inputs':op.inputs_ref,'policy':op.policy_ref,'review':review_ref,
                     'proof':h.checked(review_ref)['inverse_proof']}[kind]
                path=Path(ref['path']);before=path.read_bytes()
                path.write_bytes(before.replace(b'{',b'[',1))  # equal length
                with self.assertRaises(ValueError):op._operator_binding(op.release_action)
                path.write_bytes(before)
                with self.assertRaisesRegex(ValueError,'release_operator_binding_invalid'):
                    op._operator_binding(op.release_action)

    def test_in_memory_objects_and_reference_drift_invalidate(self):
        for key in ('inputs','policy','inputs_ref','policy_ref'):
            with self.subTest(key=key):
                op=self.fixture().bind();before=copy.deepcopy(getattr(op,key))
                getattr(op,key)['unexpected']=True
                with self.assertRaises(ValueError):op._operator_binding(op.release_action)
                setattr(op,key,before)
                with self.assertRaisesRegex(ValueError,'release_operator_binding_invalid'):
                    op._operator_binding(op.release_action)

    def test_loaded_operator_path_replacement_cannot_be_restored_to_reuse_binding(self):
        f=self.fixture();op=f.bind();alias=f.root/'operator-copy.py';alias.write_bytes(Path(r.__file__).read_bytes())
        with patch.object(r,'__file__',str(alias)):
            with self.assertRaisesRegex(ValueError,'release_operator_source_changed'):
                op._operator_binding(op.release_action)
        with self.assertRaisesRegex(ValueError,'release_operator_binding_invalid'):
            op._operator_binding(op.release_action)

    def test_each_loaded_base_path_is_exact_even_for_identical_bytes(self):
        for module in r.BASE_MODULES:
            with self.subTest(module=module.__name__):
                f=self.fixture();op=f.bind();alias=f.root/'base-copy.py';alias.write_bytes(Path(module.__file__).read_bytes())
                with patch.object(module,'__file__',str(alias)):
                    with self.assertRaisesRegex(ValueError,'release_operator_base_changed'):
                        op._operator_binding(op.release_action)
                with self.assertRaisesRegex(ValueError,'release_operator_binding_invalid'):
                    op._operator_binding(op.release_action)

    def test_final_allocation_requires_original_pin_not_an_identical_alias(self):
        f=self.fixture();policy,registration=f.lineage()
        ref=policy['native_successions']['links'][-1]['allocation']['evidence']
        alias=f.root/'allocation-copy.json';alias.write_bytes(Path(ref['path']).read_bytes())
        before=copy.deepcopy((policy,registration))
        with self.assertRaisesRegex(ValueError,'release_final_allocation_changed'):
            r.validate_release_registration(registration,policy,h.checked,
                allocation_ref=h.pin(alias),allocation_cache=s.NativeAllocationCache())
        self.assertEqual((policy,registration),before)

    def test_noncanonical_reader_and_cache_type_are_refused(self):
        f=self.fixture();policy,registration=f.lineage()
        ref=policy['native_successions']['links'][-1]['allocation']['evidence']
        for reader,cache in ((lambda value:h.checked(value),s.NativeAllocationCache()),(h.checked,object())):
            with self.assertRaisesRegex(ValueError,'release_allocation_cache_scope_changed'):
                r.validate_release_registration(registration,policy,reader,
                    allocation_ref=ref,allocation_cache=cache)

    def test_binding_and_allocation_cache_are_per_instance(self):
        first=self.fixture().bind();second=self.fixture().bind()
        self.assertIsNot(first._allocation_cache(),second._allocation_cache())
        first.inputs['changed']=True
        with self.assertRaises(ValueError):first._operator_binding(first.release_action)
        second._operator_binding(second.release_action)

    def test_direct_and_cli_start_refuse_before_any_action(self):
        f=self.fixture();op=f.bind();before=sorted(str(p) for p in f.root.rglob('*'))
        with self.assertRaisesRegex(ValueError,'release_operator_start_forbidden'):op.start(execute=True)
        with self.assertRaisesRegex(ValueError,'release_operator_action_forbidden'):
            op.evidence(op.start_action)
        self.assertEqual(sorted(str(p) for p in f.root.rglob('*')),before)
        result=subprocess.run([sys.executable,'-B',r.__file__,'start','--policy','{}',
            '--inputs','{}','--authority','{}','--execute'],capture_output=True,timeout=10,check=False,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode,2)
        self.assertIn(b'invalid choice',result.stderr)
        self.assertEqual(result.stdout,b'')
        self.assertEqual(sorted(str(p) for p in f.root.rglob('*')),before)


if __name__ == '__main__':unittest.main()
