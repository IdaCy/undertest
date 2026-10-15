"""Synthetic immutable queue proofs; no provider or scientific requests."""
import ast
import copy
import difflib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_derived_probe_runtime as runtime
from causes import campaign_supplement as core
from test_campaign_derived_probe_healthy_runtime import HealthyFixture


def queue_only_change(body):
    tree = ast.parse(body)
    queue = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_queue')
    lines = body.splitlines(keepends=True)
    lines.insert(queue.body[0].lineno - 1, b'    _synthetic_ready_marker = True\n')
    return b''.join(lines)


def inverse(parent, source):
    a, b = parent.splitlines(keepends=True), source.splitlines(keepends=True)
    return [[tag, a0, a1, b0, b1,
             hashlib.sha256(b''.join(a[a0:a1])).hexdigest(),
             hashlib.sha256(b''.join(b[b0:b1])).hexdigest()]
            for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, a, b).get_opcodes()
            if tag != 'equal']


class ReadyFixture(HealthyFixture):
    def __init__(self, root, *, changed_helper=False):
        root = Path(tempfile.mkdtemp(dir=root, prefix='ready-'))
        super().__init__(root)
        self.healthy = copy.deepcopy(self.descriptor)
        self.healthy_ref = self.review
        self.parent = self.new
        old = core.pinned(self.parent)
        body = queue_only_change(old)
        if changed_helper:
            body = body.replace(b'80 * 1024**3', b'81 * 1024**3')
        self.operational = self.module('ready', body)
        self.new = core.pin(self.operational.__file__)
        old_actor = core.pinned(self.healthy['completed_actor_source'])
        actor_path = root / 'ready_actor.py'
        actor_path.write_bytes(queue_only_change(old_actor))
        actor = core.pin(actor_path)
        self.sources = dict(native_queue=self.new, completed_actor=actor)
        self.parents = dict(native_queue=self.parent,
                            completed_actor=self.healthy['completed_actor_source'])
        for key in ('driver', 'owned_suffix', 'source_lineage'):
            self.parents[key] = self.save('old_' + key, dict(source=key, version=1))
            self.sources[key] = self.save('new_' + key, dict(source=key, version=2))
        self.binder_parent = self.save('old_binder', dict(binder=1))
        self.binder = self.save('new_binder', dict(binder=2))
        proofs = []
        for key in (*self.sources, 'binder'):
            parent = self.binder_parent if key == 'binder' else self.parents[key]
            source = self.binder if key == 'binder' else self.sources[key]
            proof = dict(key=key, parent=parent, source=source)
            if key in ('native_queue', 'completed_actor'):
                proof.update(line_edits=inverse(core.pinned(parent), core.pinned(source)),
                             changed_ast=['run_queue'], added_ast=[])
            proofs.append(proof)
        self.derivation = dict(proofs=proofs, binder_parent=self.binder_parent,
            only_reviewed_ready_harvest_native_delta=True,
            active_completed_actor_non_run_queue_preserved=True, unchanged_scientific_limits=True)
        self.preparation = dict(sources=self.sources, parent_sources=self.parents,
                                binder_source=self.binder)
        self.peer = dict(accepted_late_ready_harvest_runtime_source=True,
            accepted_original_owned_suffix_runtime_source=True, findings=[])
        self.adoption = dict(self.peer, root_authorized=True, runtime_authorized=False)
        self.serial = 0
        self.refresh()

    def refresh(self):
        self.serial += 1
        def save(label, obj):
            return self.save('ready_' + label + str(self.serial), obj)
        derivation = save('derivation', self.derivation)
        prep = dict(self.preparation, source_derivation=derivation)
        preparation = save('preparation', prep)
        joins = dict(prep, source_preparation=preparation)
        peer = save('peer', dict(joins, **self.peer))
        adoption = save('adoption', dict(joins, independent_review=peer, **self.adoption))
        self.descriptor = dict(schema='causes.derived_probe_ready_queue_compatibility.v1',
            scientific_source=self.old, healthy_compatibility=self.healthy_ref,
            operational_source=self.new, completed_actor_source=self.sources['completed_actor'],
            ready_derivation=derivation, ready_preparation=preparation,
            ready_review=peer, ready_adoption=adoption)
        self.review = save('compatibility', self.descriptor)


class ReadyRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_third_queue_transition_retains_scientific_identity_and_all_pins(self):
        f = ReadyFixture(self.root)
        before = copy.deepcopy(f.protocol)
        refs = runtime.compatible_queue(f.scientific, f.operational, f.old, f.new, review=f.review)
        self.assertEqual(len(refs), len({p['path'] for p in refs}))
        for ref in (f.old, f.new, f.parent, f.healthy_ref, f.review,
                    *[v for v in f.descriptor.values() if isinstance(v, dict)]):
            self.assertIn(ref, refs)
        adapter = f.build()
        self.assertEqual(adapter.prepare(f.job)[1]['id'], 'evaluation_presence')
        self.assertEqual(adapter.protocol, before)
        self.assertEqual(adapter.protocol['sources']['campaign_supplement'], f.old)

    def test_ready_peer_cannot_bless_request_helper_change(self):
        f = ReadyFixture(self.root, changed_helper=True)
        with self.assertRaisesRegex(f.operational.IntegrityError, 'request_helpers_changed'):
            f.build()

    def test_ready_and_original_source_flags_are_both_required(self):
        for flag in ('accepted_late_ready_harvest_runtime_source',
                     'accepted_original_owned_suffix_runtime_source'):
            for target in ('peer', 'adoption'):
                with self.subTest(flag=flag, target=target):
                    f = ReadyFixture(self.root)
                    getattr(f, target)[flag] = False
                    f.refresh()
                    with self.assertRaisesRegex(f.operational.IntegrityError, 'source_acceptance'):
                        f.build()

    def test_runtime_authority_or_findings_in_source_adoption_refuse(self):
        for change in ({'runtime_authorized': True}, {'root_authorized': False}, {'findings': ['issue']}):
            f = ReadyFixture(self.root)
            f.adoption.update(change)
            f.refresh()
            with self.subTest(change=change), self.assertRaises(f.operational.IntegrityError):
                f.build()

    def test_native_and_actor_parents_must_join_healthy_descriptor(self):
        for key in ('native_queue', 'completed_actor'):
            f = ReadyFixture(self.root)
            f.parents[key] = f.old
            f.refresh()
            with self.subTest(key=key), self.assertRaisesRegex(f.operational.IntegrityError, 'source_chain'):
                f.build()

    def test_inverse_hash_range_and_roster_are_authenticated(self):
        for change in ('hash', 'range', 'duplicate', 'missing'):
            f = ReadyFixture(self.root)
            proof = f.derivation['proofs'][0]
            if change == 'hash':
                proof['line_edits'][0][-1] = '0' * 64
            elif change == 'range':
                proof['line_edits'][0][2] = 10**8
            elif change == 'duplicate':
                f.derivation['proofs'].append(copy.deepcopy(proof))
            else:
                f.derivation['proofs'].pop()
            f.refresh()
            with self.subTest(change=change), self.assertRaises(f.operational.IntegrityError):
                f.build()

    def test_hidden_descriptor_field_and_wrong_endpoint_refuse(self):
        for changes in ({'ignore_history': True}, {'operational_source': None}):
            f = ReadyFixture(self.root)
            f.scope_change(**changes)
            with self.subTest(changes=changes), self.assertRaises(f.operational.IntegrityError):
                f.build()

    def test_healthy_history_is_not_replaced_by_ready_acceptance(self):
        f = ReadyFixture(self.root)
        healthy = json.loads(core.pinned(f.healthy_ref))
        healthy['early_review'] = f.save('not_an_early_proof', {})
        f.healthy_ref = f.save('broken_healthy', healthy)
        f.refresh()
        with self.assertRaises(f.operational.IntegrityError):
            f.build()

    def test_changed_body_and_conflicting_pin_refuse(self):
        f = ReadyFixture(self.root)
        Path(f.descriptor['ready_derivation']['path']).write_bytes(b'{}')
        with self.assertRaises((core.IntegrityError, f.operational.IntegrityError)):
            f.build()
        f = ReadyFixture(self.root)
        f.derivation['retained_extra'] = dict(f.new, sha256='0' * 64)
        f.refresh()
        with self.assertRaisesRegex(f.operational.IntegrityError, 'conflicting_pin'):
            f.build()

    def test_integrity_translation_and_no_retry_remain_literal(self):
        f = ReadyFixture(self.root)
        adapter = f.build()
        for error in (f.scientific.IntegrityError('source'),
                      f.scientific.AdmissionHold('expired'), RuntimeError('failure')):
            f.prepare_error = error
            expected = f.operational.IntegrityError if isinstance(error, f.scientific.IntegrityError) else type(error)
            with self.assertRaises(expected) as caught:
                adapter.prepare(f.job)
            self.assertTrue(caught.exception is error or caught.exception.__cause__ is error)
        self.assertEqual(len([c for c in f.calls if c[0] == 'prepare']), 3)


if __name__ == '__main__':
    unittest.main()
