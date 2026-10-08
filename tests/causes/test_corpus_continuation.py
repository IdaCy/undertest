import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes.corpus_allocation import SeedLedger, digest, key, chosen_alias, admissible_cells
from causes.corpus_continuation import CorpusContinuation
from causes.campaign_dispatcher import Dispatcher, save_new
from tests.causes.test_campaign_dispatcher import finish


FAMILY = 'corpus_fixture_continuous'


def allocation(identifier):
    return dict(run_id=identifier, seed=1, block_seed=2, task_seed=3, model_seed=4,
                continue_to_end=False, instruments=['evaluation_presence'],
                reader_stages=[], reader_thresholds=[], primary_memberships=[],
                fixed_study_inference=False, paired_stage_study=False,
                other_awareness_study=False, replacement_for_prior_assignment=False,
                source_variant_bundle_sha256='a' * 64)


def fixture():
    cells, prototypes, originals = [], [], []
    for task, count in [('one', 2), ('two', 3)]:
        for number in range(count):
            variant = {'id': f'V{number}', 'request': f'fixture {task} {number}',
                       'definition_sha256': digest([task, number]), 'eligible': True}
            variant['stimulus_sha256'] = digest({k: variant.get(k) for k in ('request', 'history', 'workspace', 'files', 'env')})
            alias = dict(source_id='bank:' + task, variant_id=variant['id'], alias_of=None,
                         definition_sha256=variant['definition_sha256'])
            cell = dict(task_id=task, stimulus_sha256=variant['stimulus_sha256'], hold_ids=[],
                        currently_admissible=True, source_aliases=[alias],
                        earliest_accepted_canonical_source=alias['source_id'],
                        earliest_accepted_canonical_variant_id=variant['id'])
            cells.append(cell)
            proto = dict(spec={'task_id': task, 'image': 'sha256:fixture'}, variant=variant,
                         allocation=allocation(f'bank_{task}_{number}'))
            prototypes.append(proto)
            original = copy.deepcopy(proto)
            original['allocation'].update(run_id=f'original_{task}_{number}', source_bank83_run_id=proto['allocation']['run_id'])
            originals.append(original)
    held = copy.deepcopy(cells[0])
    held.update(stimulus_sha256=digest('held'), currently_admissible=False, hold_ids=['hold'])
    cells.append(held)
    reservation = dict(schema='causes.corpus_allocation_reservation.v1',
                       run_ids=[j['allocation']['run_id'] for j in prototypes + originals],
                       allocation_seeds=[1, 2, 3, 4])
    config = {'model': 'fixture', 'endpoint': 'http://localhost:1', 'dispatcher': {'disk_floor_bytes': 0}}
    return dict(catalog={'physical_cells': cells}, prototypes=prototypes, original_jobs=originals,
                config=config, reservation=reservation, family=FAMILY, model_key='mi27',
                task_host='fixture', provider_host='fixture', config_sha256=digest(config))


class ContinuationTests(unittest.TestCase):
    def test_two_blocks_preserve_stimuli_and_interleave_without_reusing_allocations(self):
        args = fixture()
        before = copy.deepcopy(args)
        allocator = CorpusContinuation(**args)
        one = allocator.block(0)
        allocator.accept(one)
        two = allocator.block(1)
        self.assertEqual(args, before)
        by_key = {(j['spec']['task_id'], j['variant']['stimulus_sha256']): j for j in args['prototypes']}
        for index, block in enumerate((one, two)):
            jobs = block['jobs']
            self.assertEqual(len(jobs), 5)
            self.assertEqual({j['spec']['task_id'] for j in jobs[:2]}, {'one', 'two'})
            self.assertEqual({j['spec']['task_id'] for j in jobs[2:4]}, {'one', 'two'})
            self.assertEqual(len(block['seed_ledger']), 9)
            for position, job in enumerate(jobs):
                source = by_key[(job['spec']['task_id'], job['variant']['stimulus_sha256'])]
                self.assertEqual((job['spec'], job['variant']), (source['spec'], source['variant']))
                a = job['allocation']
                self.assertEqual((a['repetition'], a['block_index'], a['fixed_order_index']), (index + 1, index, position))
                self.assertFalse(a['fixed_study_inference'])
                self.assertEqual(a['reader_stages'], [])
                self.assertEqual(a['source_bank83_run_id'], source['allocation']['run_id'])
        first_seeds = {r['seed'] for r in one['seed_ledger'].values()}
        second_seeds = {r['seed'] for r in two['seed_ledger'].values()}
        self.assertFalse(first_seeds & second_seeds)
        self.assertFalse((first_seeds | second_seeds) & set(args['reservation']['allocation_seeds']))
        self.assertFalse({j['allocation']['run_id'] for j in one['jobs']} & {j['allocation']['run_id'] for j in two['jobs']})
        self.assertEqual(two['previous_block_sha256'], digest(one))

    def test_restart_reconstructs_whole_ledger_and_refuses_tampering(self):
        original = CorpusContinuation(**fixture())
        first = original.block(0)
        original.accept(first)
        second = original.block(1)
        restarted = CorpusContinuation(**fixture())
        restarted.accept(first)
        self.assertEqual(restarted.block(1), second)
        for field, value in [('seed_ledger', {}), ('previous_block_sha256', 'changed')]:
            damaged = copy.deepcopy(second)
            damaged[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                restarted.accept(damaged)
        # Python object equality treats 1 and 1.0 as equal; the frozen ledger
        # comparison must preserve their distinct serialized identities.
        damaged = copy.deepcopy(second)
        damaged['block_index'] = 1.0
        with self.assertRaises(ValueError):
            restarted.accept(damaged)
        self.assertEqual(restarted.index, 1)
        restarted.accept(second)

    def test_catalog_hold_alias_or_frozen_scope_drift_refuses(self):
        for mutate in (
            lambda a: a['catalog']['physical_cells'][0]['hold_ids'].append('new_hold'),
            lambda a: a['prototypes'].append(copy.deepcopy(a['prototypes'][0])),
            lambda a: a['prototypes'][0]['allocation'].update(fixed_study_inference=True),
            lambda a: a['original_jobs'][0]['variant'].update(request='changed'),
            lambda a: a['reservation']['allocation_seeds'].remove(1),
        ):
            args = fixture()
            mutate(args)
            with self.assertRaises(ValueError):
                CorpusContinuation(**args)

    def test_seed_domain_exhaustion_stops_instead_of_reusing_seed(self):
        with patch('causes.corpus_allocation.SEED_MODULUS', 5):
            with self.assertRaisesRegex(ValueError, 'exhausted'):
                SeedLedger({1, 2, 3, 4}, family=FAMILY).fresh('new')

    def test_original_lineage_preserves_serialized_numeric_types(self):
        for section in ('spec', 'variant'):
            args = fixture()
            args['prototypes'][0][section]['lineage_number'] = 1
            args['original_jobs'][0][section]['lineage_number'] = 1
            CorpusContinuation(**args)
            args['original_jobs'][0][section]['lineage_number'] = 1.0
            with self.subTest(section=section), self.assertRaisesRegex(
                    ValueError, 'canonical_prototype_or_original_changed'):
                CorpusContinuation(**args)

    def test_reserved_numeric_collision_uses_deterministic_skip_on_restart(self):
        args = fixture()
        collision = int(digest([FAMILY, 'block', 0, 'mi27', 0])[:16], 16) % (2**31 - 1)
        args['reservation']['allocation_seeds'].append(collision)
        first = CorpusContinuation(**args).block(0)
        second = CorpusContinuation(**args).block(0)
        self.assertEqual(first, second)
        self.assertNotEqual(first['block_seed'], collision)
        identity = json.dumps([FAMILY, 'block', 0, 'mi27'], sort_keys=True, ensure_ascii=False)
        self.assertEqual(first['seed_ledger'][identity]['collision_counter'], 1)

    def test_missing_block_and_claim_before_persistence_are_rejected(self):
        a = CorpusContinuation(**fixture())
        block = a.block(0)
        event = {'event': 'extension_block_created', 'block_index': 0, 'block_sha256': digest(block)}
        encode = lambda rows: b''.join(json.dumps(r).encode() + b'\n' for r in rows)
        with self.assertRaisesRegex(ValueError, 'missing_or_changed'):
            a.check_history(encode([event]))
        a.accept(block)
        self.assertEqual(a.check_history(b''), [0])
        claim = dict(event='claimed', run_id=block['jobs'][0]['allocation']['run_id'],
                     job_sha256=digest(block['jobs'][0]), config_sha256=digest(fixture()['config']))
        self.assertEqual(a.check_history(encode([event, claim])), [])
        for rows in ([claim, event], [event, claim, claim]):
            with self.assertRaises(ValueError):
                a.check_history(encode(rows))


def write_control(directory, args):
    import hashlib
    def pinned(name, value):
        path = directory / name
        path.write_text(json.dumps(value))
        raw = path.read_bytes()
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    control = {'schema': 'causes.corpus_continuation_control.v1',
               **{k: args[k] for k in ('family', 'model_key', 'task_host', 'provider_host')},
               'repetition_start': 1}
    for k in ('catalog', 'prototypes', 'original_jobs', 'config', 'reservation'):
        control[k] = pinned(k + '.json', args[k])
    return pinned('control.json', control)


class DispatcherContinuationTests(unittest.TestCase):
    def test_control_binds_whole_prototypes_before_queue_admission(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = write_control(root, args)
            changed = copy.deepcopy(args['prototypes'])
            changed[0]['allocation']['unbound_field'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'prototypes_changed'):
                Dispatcher([], args['config'], root / 'runs', max_full=0,
                           extension_prototypes=changed, corpus_continuation=control['path'],
                           corpus_continuation_sha256=control['sha256'])

    def test_same_owner_creates_blocks_pauses_and_restarts_without_replay(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = write_control(root, args)
            pause = root / 'PAUSE'
            seen = []
            def runner(spec, variant, config, allocation, output):
                seen.append(allocation['run_id'])
                result = finish(spec, variant, config, allocation, output)
                if len(seen) == 10:
                    pause.touch()
                return result
            kwargs = dict(workers=1, max_full=0, extension_prototypes=args['prototypes'],
                          pause_file=pause, runner=runner, poll_seconds=0.001,
                          corpus_continuation=control['path'], corpus_continuation_sha256=control['sha256'])
            output = root / 'runs'
            result = Dispatcher([], args['config'], output, **kwargs).run()
            self.assertEqual(result['next_extension_block'], 2)
            self.assertEqual(len(set(seen)), 10)
            restarted = Dispatcher([], args['config'], output, **kwargs)
            restarted.run()
            self.assertEqual(len(seen), 10)
            blocks = sorted((output / '.dispatcher/blocks').glob('[0-9]*.json'))
            self.assertEqual(len(blocks), 2)
            # A deleted tail ledger cannot silently regenerate claimed work.
            blocks[-1].unlink()
            with self.assertRaises(ValueError):
                Dispatcher([], args['config'], output, **kwargs).run()

    def test_durable_block_without_creation_event_is_bound_before_first_claim(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = write_control(root, args)
            pause = root / 'PAUSE'
            def runner(spec, variant, config, allocation, output):
                pause.touch()
                return finish(spec, variant, config, allocation, output)
            d = Dispatcher([], args['config'], root / 'runs', workers=1, max_full=0,
                           extension_prototypes=args['prototypes'], pause_file=pause,
                           runner=runner, poll_seconds=0.001,
                           corpus_continuation=control['path'], corpus_continuation_sha256=control['sha256'])
            save_new(d.meta / 'blocks/000000000000.json', d.continuation.block(0))
            d.run()
            rows = [json.loads(line) for line in (d.meta / 'journal.jsonl').read_bytes().splitlines()]
            created = next(i for i, r in enumerate(rows) if r['event'] == 'extension_block_created')
            claimed = next(i for i, r in enumerate(rows) if r['event'] == 'claimed')
            self.assertLess(created, claimed)
            self.assertTrue(rows[created]['recovered_persisted_unclaimed_block'])


class PublishedCatalogContinuationTests(unittest.TestCase):
    def test_full_public_catalog_two_repetitions_have_exact_held_scope_and_task_cycles(self):
        bank = Path(__file__).resolve().parents[2] / 'data/causes/task_bank/v1'
        catalog = json.loads((bank / 'catalog.json').read_bytes())
        sources = {b['source_id']: b for b in (json.loads(p.read_bytes()) for p in (bank / 'variants').rglob('*.json'))}
        prototypes, originals = [], []
        for cell in admissible_cells(catalog):
            alias = chosen_alias(cell)
            variant = sources[alias['source_id']]['variants'][alias['row_index']]
            spec = json.loads((bank / 'tasks' / (cell['task_id'] + '.json')).read_bytes())['spec']
            identifier = digest(key(cell))
            proto = dict(spec=spec, variant=variant, allocation=allocation('bank_' + identifier))
            prototypes.append(proto)
            original = copy.deepcopy(proto)
            original['allocation'].update(run_id='original_' + identifier, source_bank83_run_id=proto['allocation']['run_id'])
            originals.append(original)
        args = fixture()
        args.update(catalog=catalog, prototypes=prototypes, original_jobs=originals,
                    family='corpus_jp27_continuous_v1',
                    reservation=dict(schema='causes.corpus_allocation_reservation.v1',
                                     run_ids=[j['allocation']['run_id'] for j in prototypes + originals],
                                     allocation_seeds=[1, 2, 3, 4]))
        a = CorpusContinuation(**args)
        all_seeds = set()
        for index in range(2):
            block = a.block(index)
            self.assertEqual(len(block['jobs']), 4477)
            self.assertEqual(len(block['seed_ledger']), 4562)
            for cycle in range(43):
                self.assertEqual(len({j['spec']['task_id'] for j in block['jobs'] if j['allocation']['task_cycle'] == cycle}), 83)
            seeds = {r['seed'] for r in block['seed_ledger'].values()}
            self.assertFalse(all_seeds & seeds)
            all_seeds.update(seeds)
            self.assertEqual({(j['spec']['task_id'], j['variant']['stimulus_sha256']) for j in block['jobs']}, {key(c) for c in admissible_cells(catalog)})
            a.accept(block)
        self.assertEqual(len(all_seeds), 9124)


if __name__ == '__main__':
    unittest.main()
