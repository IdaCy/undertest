import copy
import ast
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest

from causes import campaign_supplement_closed_union as union
from causes import campaign_supplement_pair_pipeline as pipeline
from causes import campaign_supplement_projection as projector
from shared import evidence_bytes
import test_campaign_supplement_pair_pipeline as legacy_tests
import test_campaign_supplement_projection as projection_tests


class Fixture:
    """Native-shaped legacy prefix plus a distinct completed-actor invocation."""
    def __init__(self):
        self.docs, self.raw = {}, {}
        self.inputs = projection_tests.inputs(('old', 'new', 'pending'))
        jobs = self.inputs['jobs']
        for job, row in zip(jobs, self.inputs['verified_finals']):
            job.update(frozen_config=projection_tests.ref('config'), original_refs={'original': True})
            row['input_canonical_sha256'] = union.digest(job)
        self.jobs = self.put('jobs', jobs)
        sources = {key: self.put(key + '.py', {}) for key in ('collector', 'runner', 'adapter')}
        self.sources = sources
        self.old_plan = self.put('old-plan', dict(jobs=self.jobs, endpoint='http://offline',
                                                output_root='/evidence/old'))
        self.old_rows = self.put('old-rows', self.inputs['verified_finals'][:2])
        self.old = self.put('old-actual', dict(schema='causes.finite_supplement_tranche_closed.v1',
            role='mi', active=0, ambiguous=[], full_tranche_qualified=True, qualification_findings=[],
            native_collector=sources['collector'], native_runner=sources['runner'], adapter=sources['adapter'],
            plan=self.old_plan, output='/evidence/old', identity=self.put('old-identity', {}),
            verified_finals=self.old_rows, complete_ids=[j['measurement_id'] for j in jobs[:2]],
            full_jobs=self.jobs, terminal=2, new_finals_verified=2, consecutive_failures=0,
            cumulative_invalid_ids=[], journal=self.journal('old-journal', range(2))))
        self.old_peer = self.put('old-peer', dict(accepted_bounded_finite_tranche_closed=True,
                                                actual=self.old, findings=[]))
        self.constructor = self.put('constructor', {'bindings': sources})
        self.source = self.put('audit.py', {})
        self.plan = self.put('new-plan', dict(schema='causes.completed_actor_driver_plan.v1',
            mode='completed_actor_direct_probe', output_root='/evidence/new', endpoint='http://offline',
            predecessor_custody=dict(actual=self.old, review=self.old_peer),
            occupied_ids=[j['measurement_id'] for j in jobs[:2]], initial_failures=0,
            maximum_new_requests=2, jobs_sha256=union.digest(jobs[2:4]), constructor=self.constructor,
            sources=dict(driver=self.put('driver.py', {}), native_queue=self.put('queue.py', {}),
                         collector=self.put('transport.py', {}))))
        self.result = self.put('driver-result', dict(result=dict(phase='completed_actor_direct_probe',
            active=0, existing_final=0, new_final=2, remaining_unclaimed=0, stop_reason=None,
            consecutive_failures=0, completed_ids=[j['measurement_id'] for j in jobs[2:4]])))
        self.recipe = self.put('recipe', dict(source=self.source, plan=self.plan,
            driver_result=self.result, native_collector=sources['collector']))
        self.rows = self.put('new-rows', self.inputs['verified_finals'][2:4])
        self.source_pins = self.put('source-pins', [self.source, sources['runner'], sources['adapter']])
        self.actual = self.put('actual', dict(schema='causes.finite_supplement_tranche_closed.v1',
            mode='completed_actor_direct_probe', role='mi', active=0, ambiguous=[],
            full_tranche_qualified=False, clean_native_ending=True, unresolved_claimed_ids=[],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            source=self.source, plan=self.plan, output='/evidence/new',
            prior_closed=self.old, prior_closed_review=self.old_peer, full_jobs=self.jobs,
            inherited_qualification_findings=[], current_qualification_findings=[], qualification_findings=[],
            recipe=self.recipe, native_result=self.result, source_pins=self.source_pins,
            verified_finals=self.rows, complete_ids=[j['measurement_id'] for j in jobs[:4]],
            terminal=4, occupied=4, unstarted_ids=[j['measurement_id'] for j in jobs[4:]],
            stop_reason=None, consecutive_failures=0, journal=self.journal('new-journal', range(2, 4)),
            new_scientifically_qualified_ids=[j['measurement_id'] for j in jobs[2:4]],
            new_invalid_ids=[], prior_invalid_ids=[], cumulative_invalid_ids=[], scientifically_qualified_total=4))
        self.peer = self.put('peer', dict(accepted_finite_supplement_closed_custody=True,
            accepted_completed_actor_saved_closure=True, actual=self.actual, source=self.source, findings=[],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            qualification_findings=[], qualified_admission_ids=[j['measurement_id'] for j in jobs[2:4]],
            invalid_admission_ids=[], invalid_admission_custody=[]))
        self.descriptor = dict(schema='causes.supplement_closed_union.v1', maximum_payload_bytes=1024**2,
                               roots=[dict(role='mi', actual=self.actual, review=self.peer)])

    def put(self, name, value):
        raw = union.encoded(value)
        ref = dict(path='/fixture/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.docs[ref['path']] = copy.deepcopy(value); self.raw[ref['path']] = raw
        return ref

    def journal(self, name, indices):
        events, count = [], 0
        for index in indices:
            row = self.inputs['verified_finals'][index]
            events.append(dict(event='claimed', measurement_id=row['measurement_id']))
            term = copy.deepcopy(self.inputs['terminal_membership'][index]['terminal'])
            count = 0 if term['event'] == 'route_success' else count + 1
            term['consecutive_failures'] = count
            events.append(term)
        raw = b''.join(union.encoded(e) + b'\n' for e in events)
        ref = dict(path='/fixture/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.raw[ref['path']] = raw
        return ref

    def get(self, ref):
        return copy.deepcopy(self.docs[ref['path']])

    def normalize(self):
        return union.normalize_closed_union(self.descriptor, self.get,
            lambda ref: self.raw[ref['path']], pipeline.closed_lineage_rows)

    def membership(self, normalized):
        jobs = self.get(self.jobs)
        assignments = self.put('assignments', {'epochs': self.inputs['epochs']})
        peer = self.put('assignment-peer', dict(accepted_assignments=True, assignments=assignments, findings=[]))
        members = []
        for index, (row, origin) in enumerate(zip(normalized['rows'], normalized['origins'])):
            job = jobs[index]
            inp = dict(path=str(Path(row['final']['path']).parent / 'INPUT.json'),
                       bytes=len(union.encoded(job)), sha256=union.digest(job))
            self.docs[inp['path']] = copy.deepcopy(job)
            member = copy.deepcopy(self.inputs['terminal_membership'][index])
            member.update(role='mi', jobs=self.jobs, endpoint='http://offline', input=inp,
                job_index=index, global_job_ordinal=index, global_job_sha256=union.digest(job),
                frozen_config=job['frozen_config'], original_refs=job['original_refs'],
                terminal=origin['terminal'])
            members.append(member)
        membership = self.put('membership', dict(rows=members, measurement_ids=[m['measurement_id'] for m in members],
            global_jobs=self.jobs, assignments=assignments, active_rows=[]))
        return dict(global_jobs=self.jobs, assignments=assignments, assignment_review=peer,
            assignment_acceptance_flag='accepted_assignments', membership=membership,
            parts=[dict(role='mi', actual=self.actual, review=self.peer, plan=self.plan, jobs=self.jobs,
                        output_root='/evidence/new', endpoint='http://offline', terminal_count=4)])

    def invalid_admission(self):
        actual, peer = self.docs[self.actual['path']], self.docs[self.peer['path']]
        mid = actual['new_scientifically_qualified_ids'][0]
        finding = dict(measurement_id=mid, stage='native_admission', error='original_stale_claim')
        obs = self.put('observation', {'original_timestamp': 'retained'})
        actual.update(current_qualification_findings=[finding], qualification_findings=[finding],
            new_invalid_ids=[mid], cumulative_invalid_ids=[mid], scientifically_qualified_total=3,
            new_scientifically_qualified_ids=actual['new_scientifically_qualified_ids'][1:])
        peer.update(qualification_findings=[finding], invalid_admission_ids=[mid],
            qualified_admission_ids=peer['qualified_admission_ids'][1:],
            invalid_admission_custody=[dict(measurement_id=mid, observation=obs, native_saved_observation=obs,
                                           scientific_qualified=False, request_replay_permitted=False)])


class ClosedUnionTests(unittest.TestCase):
    def test_actual_pipeline_opt_in_loads_pinned_codec_from_cold_or_stale_namespace(self):
        tree = ast.parse(Path(pipeline.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        block = next(n for n in main.body if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "'normalization' in binding")
        code = compile(ast.Module(body=[block], type_ignores=[]), pipeline.__file__, 'exec')
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as directory:
                old_path = list(sys.path)
                old_modules = {k: v for k, v in sys.modules.items() if k == 'shared' or k.startswith('shared.')}
                try:
                    for name in old_modules: del sys.modules[name]
                    if stale:
                        sys.modules['shared'] = types.ModuleType('shared')
                        wrong = types.ModuleType('shared.evidence_bytes'); wrong.__file__ = '/wrong/codec.py'
                        sys.modules['shared.evidence_bytes'] = wrong
                        sys.modules['shared'].evidence_bytes = wrong
                    source = pipeline.pin(Path(union.__file__))
                    peer = Path(directory) / 'peer.json'
                    peer.write_text(json.dumps(dict(accepted_supplement_closed_union_source=True,
                                                    source=source, findings=[])))
                    binding = dict(normalization={}, normalization_source=source,
                        normalization_source_review=pipeline.pin(peer), exact_bytes_source=pipeline.pin(Path(evidence_bytes.__file__)))
                    scope = dict(vars(pipeline), binding=binding, mode='continue')
                    exec(code, scope)
                    self.assertEqual(pipeline.pin(scope['normalizer'].evidence_bytes.__file__), binding['exact_bytes_source'])
                finally:
                    for name in list(sys.modules):
                        if name == 'shared' or name.startswith('shared.'): del sys.modules[name]
                    sys.modules.update(old_modules); sys.path[:] = old_path

    def test_native_rows_are_literal_with_per_invocation_origins(self):
        f = Fixture(); before = copy.deepcopy(f.docs); value = f.normalize()
        self.assertEqual(value['rows'], f.get(f.old_rows) + f.get(f.rows))
        self.assertEqual(f.docs, before)
        self.assertEqual([o['verified_index'] for o in value['origins']], [0, 1, 0, 1])
        self.assertEqual([o['output_root'] for o in value['origins']], ['/evidence/old'] * 2 + ['/evidence/new'] * 2)
        self.assertEqual(value['origins'][2]['sources']['collector'], f.sources['collector'])
        self.assertNotEqual(value['origins'][2]['sources']['collector'],
                            value['origins'][2]['invocation']['transport'])

    def test_membership_to_native_projector_preserves_missing_response_and_admission(self):
        f = Fixture(); f.invalid_admission()
        projection_tests.make_missing(f.inputs, index=3, parsed=True)
        f.docs[f.rows['path']][1] = copy.deepcopy(f.inputs['verified_finals'][3])
        f.docs[f.actual['path']]['journal'] = f.journal('new-failed-journal', range(2, 4))
        f.docs[f.actual['path']]['consecutive_failures'] = 1
        f.docs[f.result['path']]['result']['consecutive_failures'] = 1
        normalized = f.normalize(); data = f.membership(normalized)
        jobs, membership, assignments, rows, origins = union.validate_membership(data, normalized, f.get)
        evidence = union.projected_evidence(data['membership'], membership, projection_tests.ref('combined'), rows, origins)
        self.assertTrue(all('measurement_id' not in origin for origin in origins))
        invalid = pipeline.invalid_admission_proofs(rows, origins)
        for mid, proof in invalid.items(): evidence[mid]['invalid_admission'] = proof
        before = copy.deepcopy(rows)
        projected = projector.project_verified_supplement_pairs(jobs=jobs, epochs=assignments['epochs'],
            terminal_membership=membership['rows'], verified_finals=rows,
            occupied_rows=f.inputs['occupied_rows'], evidence_by_measurement=evidence, invalid_admissions=invalid)
        self.assertEqual(projected['counts'], dict(contexts=2, occupied=4, observed=2, missing=2))
        self.assertEqual(rows, before)
        self.assertEqual(rows[2]['label'], 'yes')
        self.assertEqual(rows[3]['raw_parsed_label'], 'invalid')
        self.assertEqual(projected['slots'][2]['reason'], 'invalid_native_admission:original_stale_claim')
        self.assertEqual(projected['slots'][3]['reason'], 'preserved failure')

    def test_legacy_and_normalized_origin_identity_produce_literal_invalid_proofs(self):
        legacy = legacy_tests.PartialQualificationTests()
        legacy_rows, legacy_origins = legacy.call(legacy.fixture())
        normalized = Fixture(); normalized.invalid_admission()
        value = normalized.normalize()
        for rows, origins in ((legacy_rows, legacy_origins), (value['rows'], value['origins'])):
            before = copy.deepcopy((rows, origins))
            invalid = pipeline.invalid_admission_proofs(rows, origins)
            self.assertEqual(len(invalid), 1)
            mid, proof = next(iter(invalid.items()))
            index = next(i for i, row in enumerate(rows) if row['measurement_id'] == mid)
            self.assertIs(proof, origins[index]['admission_qualification'])
            self.assertEqual(proof['row_sha256'], union.digest(rows[index]))
            self.assertEqual((rows, origins), before)

    def test_invalid_origin_or_proof_cannot_be_reassigned_to_another_row(self):
        for mutation in ('short', 'order', 'duplicate', 'origin_id', 'origin_hash',
                         'proof_id', 'proof_hash', 'proof_artifact', 'proof_index', 'bool_index'):
            with self.subTest(mutation=mutation):
                fixture = Fixture(); fixture.invalid_admission(); value = fixture.normalize()
                rows, origins = value['rows'], value['origins']
                origin = origins[2]; proof = origin['admission_qualification']
                if mutation == 'short': origins.pop()
                elif mutation == 'order': origins[0], origins[2] = origins[2], origins[0]
                elif mutation == 'duplicate': rows[0] = copy.deepcopy(rows[2])
                elif mutation == 'origin_id': origin['measurement_id'] = rows[0]['measurement_id']
                elif mutation == 'origin_hash': origin['row_sha256'] = union.digest(rows[0])
                elif mutation == 'proof_id': proof['measurement_id'] = rows[0]['measurement_id']
                elif mutation == 'proof_hash': proof['row_sha256'] = union.digest(rows[0])
                elif mutation == 'proof_artifact': proof['verified_finals'] = origins[0]['verified_finals']
                elif mutation == 'proof_index': proof['verified_index'] += 1
                else: proof['verified_index'] = False
                with self.assertRaises(ValueError): pipeline.invalid_admission_proofs(rows, origins)

    def test_actual_main_uses_checked_origin_join_before_projector(self):
        tree = ast.parse(Path(pipeline.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'invalid_admissions' for t in n.targets))
        code = compile(ast.Module(body=main.body[start:start + 2], type_ignores=[]), pipeline.__file__, 'exec')
        fixture = Fixture(); fixture.invalid_admission(); value = fixture.normalize()
        evidence = {r['measurement_id']: {} for r in value['rows']}
        scope = dict(vars(pipeline), verified=value['rows'], origins=value['origins'], evidence=evidence)
        exec(code, scope)
        self.assertEqual(len(scope['invalid_admissions']), 1)
        mid, proof = next(iter(scope['invalid_admissions'].items()))
        self.assertEqual(evidence[mid]['invalid_admission'], proof)
        self.assertIsNot(evidence[mid]['invalid_admission'], proof)

    def test_custody_parent_source_and_denominator_cannot_change(self):
        changes = [('actual', 'prior_closed_review', {}), ('actual', 'full_jobs', {}),
            ('actual', 'complete_ids', []), ('actual', 'unstarted_ids', []),
            ('actual', 'all_original_attempts_retained', False), ('actual', 'active', True),
            ('actual', 'terminal', True), ('actual', 'mode', 'other'),
            ('plan', 'occupied_ids', []), ('plan', 'jobs_sha256', '0' * 64),
            ('plan', 'output_root', '/other'), ('plan', 'initial_failures', 1),
            ('peer', 'accepted_completed_actor_saved_closure', False), ('peer', 'source', {}),
            ('peer', 'qualification_findings', [{}]), ('recipe', 'native_collector', {}),
            ('recipe', 'source', {}), ('recipe', 'driver_result', {})]
        for scope, key, value in changes:
            with self.subTest(scope=scope, key=key):
                f = Fixture(); f.docs[getattr(f, scope)['path']][key] = value
                with self.assertRaises((ValueError, KeyError)): f.normalize()

    def test_native_result_counts_failures_order_and_journal_are_not_reset(self):
        for field, value in [('active', 1), ('new_final', True), ('existing_final', 1),
                             ('remaining_unclaimed', 1), ('consecutive_failures', 1),
                             ('completed_ids', []), ('stop_reason', 'different')]:
            with self.subTest(field=field):
                f = Fixture(); f.docs[f.result['path']]['result'][field] = value
                with self.assertRaises(ValueError): f.normalize()
        for change in ('duplicate_claim', 'duplicate_terminal', 'counter', 'final', 'order'):
            with self.subTest(change=change):
                f = Fixture(); ref = f.docs[f.actual['path']]['journal']
                events = [json.loads(line) for line in f.raw[ref['path']].splitlines()]
                if change == 'duplicate_claim': events.append(events[0])
                elif change == 'duplicate_terminal': events.append(events[1])
                elif change == 'counter': events[1]['consecutive_failures'] = 9
                elif change == 'final': events[1]['final'] = {}
                else: events = events[2:] + events[:2]
                f.raw[ref['path']] = b'\n'.join(union.encoded(e) for e in events)
                with self.assertRaises(ValueError): f.normalize()

    def test_invalid_claim_cannot_be_promoted_or_rewritten(self):
        for change in ('observation', 'finding', 'missing', 'replay', 'prior'):
            with self.subTest(change=change):
                f = Fixture(); f.invalid_admission(); peer = f.docs[f.peer['path']]
                if change == 'observation':
                    peer['invalid_admission_custody'][0]['native_saved_observation'] = f.put('wrong-obs', {})
                elif change == 'finding': peer['qualification_findings'] = []
                elif change == 'missing': peer['invalid_admission_ids'] = []
                elif change == 'replay': peer['invalid_admission_custody'][0]['request_replay_permitted'] = True
                else: f.docs[f.actual['path']]['prior_invalid_ids'] = ['invented']
                with self.assertRaises(ValueError): f.normalize()

    def test_membership_requires_original_input_job_role_and_invocation(self):
        for change in ('input_path', 'job_index', 'ordinal', 'hash', 'role', 'endpoint', 'terminal',
                       'part', 'part_bank', 'part_count', 'part_role', 'active', 'order'):
            with self.subTest(change=change):
                f = Fixture(); normalized = f.normalize(); data = f.membership(normalized)
                membership = f.docs[data['membership']['path']]; member = membership['rows'][2]
                if change == 'input_path':
                    old = member['input']; member['input'] = f.put('copied-input', f.get(old))
                elif change == 'job_index': member['job_index'] = 0
                elif change == 'ordinal': member['global_job_ordinal'] = True
                elif change == 'hash': member['global_job_sha256'] = 'f' * 64
                elif change in ('role', 'endpoint'): member[change] = 'other'
                elif change == 'terminal': member['terminal']['final'] = {}
                elif change == 'part': data['parts'][0]['plan'] = f.old_plan
                elif change == 'part_bank': data['parts'][0]['jobs'] = f.rows
                elif change == 'part_count': data['parts'][0]['terminal_count'] = True
                elif change == 'part_role': data['parts'][0]['role'] = 'foreign'
                elif change == 'active': membership['active_rows'] = [member]
                else: membership['measurement_ids'].reverse()
                with self.assertRaises(ValueError): union.validate_membership(data, normalized, f.get)

    def test_duplicate_roles_ancestry_or_scientific_ids_refuse(self):
        f = Fixture(); f.descriptor['roots'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate_role'): f.normalize()
        f = Fixture(); f.docs[f.actual['path']]['prior_closed'] = f.actual
        f.docs[f.actual['path']]['prior_closed_review'] = f.peer
        f.docs[f.plan['path']]['predecessor_custody'] = dict(actual=f.actual, review=f.peer)
        f.docs[f.plan['path']]['occupied_ids'] = f.docs[f.actual['path']]['complete_ids']
        with self.assertRaisesRegex(ValueError, 'cycle'): f.normalize()
        f = Fixture(); f.docs[f.rows['path']].append(f.docs[f.rows['path']][0])
        with self.assertRaisesRegex(ValueError, 'duplicate_row'): f.normalize()

    def test_completed_actor_cannot_skip_an_unclaimed_selected_job(self):
        f = Fixture(); actual = f.docs[f.actual['path']]
        second = f.docs[f.rows['path']][1]
        f.docs[f.rows['path']] = [second]
        actual.update(complete_ids=actual['complete_ids'][:2] + [second['measurement_id']],
                      terminal=3, occupied=3)
        actual['unstarted_ids'] = [j['measurement_id'] for j in f.get(f.jobs)
                                   if j['measurement_id'] not in actual['complete_ids']]
        with self.assertRaisesRegex(ValueError, 'complete_partition'): f.normalize()

    def test_exported_evidence_rejects_truncation_reordering_or_changed_origin(self):
        for change in ('short', 'order', 'final', 'origin'):
            with self.subTest(change=change):
                f = Fixture(); normalized = f.normalize(); data = f.membership(normalized)
                _, membership, _, rows, origins = union.validate_membership(data, normalized, f.get)
                if change == 'short': origins.pop()
                elif change == 'order': membership['rows'].reverse()
                elif change == 'final': membership['rows'][0]['final'] = {}
                else: origins[0]['row_sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    union.projected_evidence(data['membership'], membership, projection_tests.ref('combined'), rows, origins)

    def test_membership_reads_each_exact_reference_only_once(self):
        f = Fixture(); normalized = f.normalize(); data = f.membership(normalized); calls = []
        def get(ref):
            calls.append(union.encoded(ref)); return f.get(ref)
        union.validate_membership(data, normalized, get)
        self.assertEqual(len(calls), len(set(calls)))

    def test_exact_gzip_preserves_rows_and_rejects_tampering_trailing_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(); raw = union.encoded(f.get(f.rows))
            record = evidence_bytes.write_gzip(Path(directory) / 'rows.gz', raw)
            wrapper = f.put('wrapper', record)
            f.docs[f.actual['path']]['verified_finals'] = wrapper
            f.raw[record['stored']['path']] = Path(record['stored']['path']).read_bytes()
            value = f.normalize()
            self.assertEqual(value['rows'][2:], json.loads(raw))
            self.assertEqual(value['origins'][2]['verified_finals'], wrapper)
            for change in ('raw_hash', 'raw_bound', 'trailing', 'duplicate_json'):
                with self.subTest(change=change):
                    altered = copy.deepcopy(record)
                    if change == 'raw_hash': altered['raw']['sha256'] = 'f' * 64
                    elif change == 'raw_bound': altered['raw']['bytes'] = 1024**2 + 1
                    else:
                        packed = (Path(record['stored']['path']).read_bytes() + b'trailing' if change == 'trailing'
                                  else gzip.compress(b'[{"x":1,"x":2}]'))
                        path = Path(directory) / change; path.write_bytes(packed)
                        altered['stored'] = evidence_bytes.pin(path)
                        if change == 'duplicate_json':
                            decoded = gzip.decompress(packed)
                            altered['raw'] = dict(bytes=len(decoded), sha256=hashlib.sha256(decoded).hexdigest())
                        f.raw[str(path)] = packed
                    f.docs[wrapper['path']] = altered
                    with self.assertRaises(ValueError): f.normalize()

    def test_plain_payload_limit_and_unknown_codec_refuse(self):
        f = Fixture(); f.descriptor['maximum_payload_bytes'] = 1
        with self.assertRaisesRegex(ValueError, 'payload_limit'): f.normalize()
        f = Fixture(); f.docs[f.rows['path']] = {'schema': 'some_gzip_guess'}
        with self.assertRaisesRegex(ValueError, 'payload_schema'): f.normalize()

    def test_declared_size_bounds_refuse_before_body_reader(self):
        f = Fixture(); ref = dict(f.rows, bytes=evidence_bytes.ABSOLUTE_LIMIT + 1)
        def unexpected(ref): self.fail('oversized body reader invoked')
        with self.assertRaises(ValueError):
            union.payload(ref, unexpected, unexpected, maximum_bytes=100)
        record = dict(schema='shared.exact_gzip_bytes.v1',
                      raw=dict(bytes=101, sha256='0' * 64), stored=f.rows)
        with self.assertRaises(ValueError):
            union.payload(f.rows, lambda ref: record, unexpected, maximum_bytes=100)


class FullBankEndingTests(unittest.TestCase):
    def fixture(self):
        f = legacy_tests.PartialQualificationTests().held_fixture()
        docs, get, refs = f[1], f[2], f[6]
        actual = docs[refs['final']['path']]; plan = docs[actual['plan']['path']]
        plan['queue_view'] = actual['queue_view'] = None
        actual['native_jobs'] = actual['full_jobs']
        actual['native_population_size'] = actual['full_population_size']
        actual['native_remaining_unclaimed'] = actual['full_remaining_unclaimed']
        docs[actual['native_result']['path']]['remaining_unclaimed'] = actual['full_remaining_unclaimed']
        return f, actual, get(actual['verified_finals'])

    def test_full_bank_is_explicit_and_keeps_all_original_rows(self):
        f, actual, rows = self.fixture(); before = copy.deepcopy(f[1])
        with self.assertRaises((ValueError, TypeError)):
            pipeline.verify_partial_ending(actual, rows, f[2])
        pipeline.verify_partial_ending(actual, rows, f[2], allow_full_bank=True)
        self.assertEqual(f[1], before)

    def test_full_bank_completion_order_can_differ_but_not_row_order(self):
        f, actual, rows = self.fixture(); docs = f[1]; native = docs[actual['native_result']['path']]
        bank = docs[actual['full_jobs']['path']]; added = bank[3]['measurement_id']
        rows.append(dict(rows[0], measurement_id=added))
        actual['complete_ids'].append(added); actual['terminal'] += 1; actual['new_finals_verified'] += 1
        actual['unstarted_ids'].pop(0); actual['native_remaining_unclaimed'] -= 1; actual['full_remaining_unclaimed'] -= 1
        native.update(new_final=2, remaining_unclaimed=1, completed_ids=[added, rows[0]['measurement_id']])
        pipeline.verify_partial_ending(actual, rows, f[2], allow_full_bank=True)
        with self.assertRaisesRegex(ValueError, 'row_order'):
            pipeline.verify_partial_ending(actual, list(reversed(rows)), f[2], allow_full_bank=True)
        native['completed_ids'] = [added, added]
        with self.assertRaises(ValueError): pipeline.verify_partial_ending(actual, rows, f[2], allow_full_bank=True)

    def test_full_bank_does_not_accept_a_different_native_subset(self):
        for field in ('native_jobs', 'native_population_size', 'native_remaining_unclaimed'):
            f, actual, rows = self.fixture()
            actual[field] = f[6]['base'] if field == 'native_jobs' else actual[field] - 1
            with self.assertRaises(ValueError): pipeline.verify_partial_ending(actual, rows, f[2], allow_full_bank=True)


class CleanSummaryReviewTests(unittest.TestCase):
    @staticmethod
    def summary(fixture, count_field='new_qualified'):
        actual = fixture.docs[fixture.actual['path']]
        peer = fixture.docs[fixture.peer['path']]
        actual['current_tranche_qualified'] = True
        for name in ('qualified_admission_ids', 'invalid_admission_ids', 'invalid_admission_custody'):
            peer.pop(name)
        peer.update(schema='causes.completed_actor_saved_closure_review.v1',
                    current_tranche_qualified=True, new_invalid_ids=[],
                    current_qualification_findings=[], inherited_qualification_findings=[],
                    cumulative_invalid_ids=[], partition=dict(active=0, new_invalid=0,
                    occupied=4, prior_occupied=2, **{count_field: 2}))
        return actual, peer

    def test_both_saved_summary_formats_preserve_rows_origins_and_inputs(self):
        for count_field in ('new_qualified', 'new_admission_qualified'):
            with self.subTest(count_field=count_field):
                f = Fixture(); expected = f.normalize()
                self.summary(f, count_field); before = copy.deepcopy(f.docs)
                self.assertEqual(f.normalize(), expected)
                self.assertEqual(f.docs, before)

    def test_summary_requires_exact_explicit_clean_current_qualification(self):
        changes = [
            ('schema', 'unknown'), ('current_tranche_qualified', False),
            ('new_invalid_ids', ['bad']), ('current_qualification_findings', ['bad']),
            ('inherited_qualification_findings', ['changed']), ('cumulative_invalid_ids', ['changed']),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                f = Fixture(); _, peer = self.summary(f); peer[field] = value
                with self.assertRaises(ValueError): f.normalize()
        f = Fixture(); actual, _ = self.summary(f); actual['current_tranche_qualified'] = False
        with self.assertRaises(ValueError): f.normalize()

    def test_summary_rejects_missing_boolean_and_conflicting_counts(self):
        for field in ('active', 'new_invalid', 'occupied', 'prior_occupied', 'new_qualified'):
            for value in (None, False, -1, 999):
                with self.subTest(field=field, value=value):
                    f = Fixture(); _, peer = self.summary(f); peer['partition'][field] = value
                    with self.assertRaises(ValueError): f.normalize()
        f = Fixture(); _, peer = self.summary(f)
        peer['partition']['new_admission_qualified'] = 2
        with self.assertRaises(ValueError): f.normalize()
        for field in ('scientifically_qualified_total', 'qualified_custody_total'):
            f = Fixture(); _, peer = self.summary(f); peer['partition'][field] = 999
            with self.assertRaises(ValueError): f.normalize()

    def test_partial_typed_review_never_falls_back_to_summary(self):
        for field in ('qualified_admission_ids', 'invalid_admission_ids', 'invalid_admission_custody'):
            for value in ([], None):
                f = Fixture(); _, peer = self.summary(f); peer[field] = value
                with self.assertRaisesRegex(ValueError, 'closed_admission_review_fields'):
                    f.normalize()

    def test_summary_cannot_promote_invalid_or_reorder_qualified_measurements(self):
        f = Fixture(); actual, _ = self.summary(f)
        actual['new_scientifically_qualified_ids'].reverse()
        with self.assertRaises(ValueError): f.normalize()
        f = Fixture(); f.invalid_admission(); self.summary(f)
        with self.assertRaises(ValueError): f.normalize()

    def test_inherited_invalids_remain_literal_in_clean_delta(self):
        f = Fixture(); actual, peer = self.summary(f)
        actual['inherited_qualification_findings'] = peer['inherited_qualification_findings'] = ['historical']
        actual['cumulative_invalid_ids'] = peer['cumulative_invalid_ids'] = ['historical-id']
        actual['scientifically_qualified_total'] = 3
        peer['partition']['scientifically_qualified_total'] = 3
        before = copy.deepcopy((actual, peer))
        ids = actual['new_scientifically_qualified_ids']
        self.assertEqual(union.admission_review_partition(actual, peer, ids, 2), (ids, [], []))
        self.assertEqual((actual, peer), before)


if __name__ == '__main__':
    unittest.main()
