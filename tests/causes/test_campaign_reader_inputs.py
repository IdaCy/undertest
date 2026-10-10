import copy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from causes import campaign_reader_watch as watch
from causes import campaign_snapshot as snapshot
from tests.causes import test_campaign_snapshot as snapshot_tests
from tests.causes import test_campaign_reader_watch as watcher_tests
from tests.causes import test_campaign_snapshot_watch as continuous_tests


class ReaderInputSnapshotTests(TestCase):
    setUp = snapshot_tests.SnapshotTests.setUp
    add_run = snapshot_tests.SnapshotTests.add_run
    transport = snapshot_tests.SnapshotTests.transport

    def fixture(self, name='selected'):
        run = self.add_run(name)
        for filename in snapshot.READER_INPUT_FILES:
            if filename != 'summary.json':
                (run / filename).write_text('{}')
        config = replace(self.config, selected_runs=(str(run.relative_to(self.remote)),),
                         reader_inputs_only=True, limit=5)
        return run, config

    def authorize(self, config):
        return dict(snapshot_tests.SnapshotTests.authorize(self, config), custody_kind='actor_reader_inputs')

    def manifest(self, config):
        request = dict(host=config.host, base=config.remote_base, roots=config.roots,
                       selected_runs=config.selected_runs, accepted=[], limit=config.limit,
                       reader_inputs_only=True)
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(request))), redirect_stdout(output):
            exec(compile(snapshot.REMOTE_CODE, '<reader-input manifest>', 'exec'), {})
        return json.loads(output.getvalue())

    def test_direct_named_reads_keep_missing_context_explicit_and_ignore_workspace(self):
        run, config = self.fixture()
        (run / 'end_context.json').unlink()
        (run / 'checkpoint.tar').write_bytes(b'opaque workspace archive')
        (run / 'sandbox').mkdir()
        original_open = Path.open
        def named_only(path, *args, **kwargs):
            if path.parent == run and path.name not in snapshot.READER_INPUT_FILES:
                self.fail('non-reader payload read')
            return original_open(path, *args, **kwargs)
        with patch('os.walk', side_effect=AssertionError('workspace traversed')), \
             patch.object(Path, 'open', named_only):
            manifest = self.manifest(config)
        snapshot.validate_manifest(manifest, config, set())
        row = manifest['runs'][0]
        self.assertEqual(manifest['schema'], 'caus.reader_input_manifest.v1')
        self.assertEqual({item['path'] for item in row['files']}, set(snapshot.READER_INPUT_FILES[:-1]))
        self.assertEqual(row['omitted'], [{'path': 'end_context.json', 'reason': 'named_reader_input_absent'}])

    def test_typed_feed_reuses_full_prefix_and_never_writes_full_index(self):
        old = self.add_run('already_held')
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport):
            snapshot.synchronize_completed(self.config, lambda c: snapshot_tests.SnapshotTests.authorize(self, c))
        index = self.config.destination / 'accepted.jsonl'
        original = index.read_bytes()
        feed = index.with_name('reader_inputs.accepted.jsonl')
        feed.write_bytes(original)
        run, config = self.fixture()
        config = replace(config, selected_runs=(str(old.relative_to(self.remote)), *config.selected_runs))
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport):
            result = snapshot.synchronize_completed(config, self.authorize)
            first = feed.read_bytes()
            second = snapshot.synchronize_completed(config, self.authorize)
        self.assertEqual(result['verified_runs'], 1)
        self.assertEqual(second['verified_runs'], 0)
        self.assertEqual(index.read_bytes(), original)
        self.assertTrue(first.startswith(original))
        self.assertEqual(feed.read_bytes(), first)
        receipt = json.loads(first[len(original):])
        self.assertEqual(receipt['schema'], 'caus.reader_input_receipt.v1')
        self.assertEqual(receipt['custody_kind'], 'actor_reader_inputs')
        self.assertEqual(receipt['files'], 8)
        self.assertEqual({p.name for p in Path(receipt['path']).iterdir()}, set(snapshot.READER_INPUT_FILES))
        self.assertEqual(json.loads(Path(result['attempt'], 'VERIFIED.json').read_bytes())['schema'],
                         'caus.reader_input_verified.v1')

    def test_missing_identity_symlink_and_oversize_refuse_before_transfer(self):
        for change in ('missing', 'symlink', 'oversize'):
            run, config = self.fixture(change)
            target = run / 'allocation.json'
            if change == 'missing':
                target.unlink()
            elif change == 'symlink':
                target.unlink(); target.symlink_to(run / 'summary.json')
            else:
                with target.open('wb') as stream:
                    stream.truncate(snapshot.READER_INPUT_FILE_LIMIT + 1)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manifest(config)

    def test_scope_and_pass_limits_reject_received_manifest_before_copy(self):
        _, config = self.fixture()
        baseline = self.manifest(config)
        mutations = []
        wrong_schema = copy.deepcopy(baseline); wrong_schema['schema'] = 'caus.completed_snapshot_manifest.v1'
        mutations.append(wrong_schema)
        unknown = copy.deepcopy(baseline); unknown['runs'][0]['files'][0]['path'] = 'checkpoint.tar'
        mutations.append(unknown)
        omitted = copy.deepcopy(baseline); omitted['runs'][0]['omitted'] = [{'path': 'end_context.json', 'reason': 'absent'}]
        mutations.append(omitted)
        excessive = copy.deepcopy(baseline)
        for row in excessive['runs'][0]['files']:
            row['bytes'] = snapshot.READER_INPUT_FILE_LIMIT
        excessive['runs'][0]['bytes'] = sum(row['bytes'] for row in excessive['runs'][0]['files'])
        mutations.append(excessive)
        for manifest in mutations:
            with self.subTest(manifest=manifest['schema']), self.assertRaises(ValueError):
                snapshot.validate_manifest(manifest, config, set())

    def test_selection_authority_and_native_disk_floor_remain_required(self):
        _, config = self.fixture()
        with patch('causes.campaign_snapshot.subprocess.run') as transport:
            with self.assertRaisesRegex(ValueError, 'exact_run_selection'):
                snapshot.synchronize_completed(replace(config, selected_runs=None), self.authorize)
            with self.assertRaisesRegex(ValueError, 'reader_input_authorization'):
                snapshot.synchronize_completed(config, lambda c: snapshot_tests.SnapshotTests.authorize(self, c))
            transport.assert_not_called()
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=self.transport), \
             patch('causes.campaign_snapshot.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError, 'free-space'):
                snapshot.synchronize_completed(replace(config, free_space_floor_bytes=1), self.authorize)
        self.assertEqual(self.transport_calls, ['ssh'])
        self.assertFalse((config.destination / config.index_name).exists())

    def test_corrupt_transfer_preserves_attempt_without_acceptance(self):
        _, config = self.fixture()
        def corrupt(command, **kwargs):
            result = self.transport(command, **kwargs)
            if command[0] == 'rsync':
                (Path(command[-1]) / config.selected_runs[0] / 'early_context.json').write_bytes(b'bad')
            return result
        with patch('causes.campaign_snapshot.subprocess.run', side_effect=corrupt), \
             self.assertRaisesRegex(RuntimeError, 'verification failed'):
            snapshot.synchronize_completed(config, self.authorize)
        self.assertFalse((config.destination / config.index_name).exists())
        self.assertEqual(len(list((config.destination / 'reader_input_attempts').glob('*/MANIFEST.json'))), 1)


class ReaderInputWatcherTests(TestCase):
    setUp = watcher_tests.ReaderWatcherTests.setUp
    refresh_manifest = watcher_tests.ReaderWatcherTests.refresh_manifest
    run_watcher = watcher_tests.ReaderWatcherTests.run_watcher

    def pin(self, path):
        return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': watch.file_sha(path)}

    def record(self, name, value):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return self.pin(path)

    def typed(self):
        value = json.loads(self.manifest.read_bytes())
        value['schema'] = 'caus.reader_input_manifest.v1'
        row = value['runs'][0]
        row['bytes'] = sum(item['bytes'] for item in row['files'])
        row['omitted'] = []
        self.manifest.write_text(json.dumps(value))
        self.receipt.update(schema='caus.reader_input_receipt.v1', custody_kind='actor_reader_inputs',
                            bytes=row['bytes'], files=len(row['files']), manifest_sha256=watch.file_sha(self.manifest))
        return self.receipt

    def succession(self):
        selected = {'fixture': self.frozen}
        old_source = self.base / 'old_watcher.py'; old_source.write_text('old source')
        old_sha = watch.file_sha(old_source)
        old_pins = {'watcher_source_sha256': old_sha, 'reader_config_sha256': 'same checker',
                    'prompts': {'rubric': 'same rubric'}, 'reader_source_sha256': 'same reader'}
        old_pins['per_arm'] = {'first': copy.deepcopy(old_pins)}
        pins = copy.deepcopy(old_pins)
        pins['watcher_source_sha256'] = pins['per_arm']['first']['watcher_source_sha256'] = watch.WATCH_SOURCE_SHA
        old_identity = {'pins': old_pins, 'selected_sha256': watch.reader.digest(selected),
                        'accepted_index': str(self.accepted), 'workers': 1}
        predecessor = self.record('reader/.watch/IDENTITY.json', old_identity)
        journal = [
            {'event': 'claimed', 'key': 'mi27/fixture/early'},
            {'event': 'finished', 'key': 'mi27/fixture/early', 'reader_status': 'technical_missing'},
            {'event': 'claimed', 'key': 'mi27/fixture/end'},
            {'event': 'finished', 'key': 'mi27/fixture/end', 'reader_status': 'reader_context_returned'},
        ]
        raw = b''.join(json.dumps(row).encode() + b'\n' for row in journal)
        baseline = self.base / 'closed_journal.jsonl'; baseline.write_bytes(raw)
        (self.output / '.watch/journal.jsonl').write_bytes(raw)
        evidence = self.record('retired_process.json', {'absent': True})
        old_watcher = self.pin(old_source)
        retired = self.record('retirement.json', {
            'accepted_reader_retirement': True, 'process_absent': True,
            'watch_lock_acquired_nonblocking': True, 'all_claimed_terminal': True,
            'ambiguous_attempts': 0, 'predecessor_identity': predecessor, 'old_watcher': old_watcher,
            'output': str(self.output), 'journal_baseline': self.pin(baseline), 'evidence': [evidence],
        })
        feed = self.accepted.with_name('reader_inputs.accepted.jsonl')
        feed.write_bytes(self.accepted.read_bytes())
        identity = dict(old_identity, pins=pins, accepted_index=str(feed))
        review = {
            'accepted_reader_input_succession': True, 'output': str(self.output),
            'predecessor_identity': predecessor, 'new_identity': identity,
            'old_watcher': old_watcher, 'new_watcher': self.pin(Path(watch.__file__).resolve()),
            'pin_replacements': [
                {'path': ['watcher_source_sha256'], 'old': old_sha, 'new': watch.WATCH_SOURCE_SHA},
                {'path': ['per_arm', 'first', 'watcher_source_sha256'], 'old': old_sha, 'new': watch.WATCH_SOURCE_SHA}],
            'accepted_baseline': self.pin(self.accepted), 'journal_baseline': self.pin(baseline),
            'retirement_review': retired,
        }
        reference = self.record('succession.json', review)
        return selected, pins, feed, identity, review, reference

    def test_typed_inputs_bind_whole_hashes_and_preserve_context_semantics(self):
        receipt = self.typed()
        binding, loaded = watch.bind_snapshot(receipt, self.frozen, self.snapshots, {})
        self.assertEqual(binding['snapshot_receipt']['custody_kind'], 'actor_reader_inputs')
        self.assertEqual(watch.stage_context('end', loaded)[0], self.end)
        (self.root / 'early_context.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'source_file_hash_mismatch'):
            watch.bind_snapshot(receipt, self.frozen, self.snapshots, {})

    def test_typed_manifest_cannot_masquerade_as_full_or_hide_missing_context(self):
        receipt = self.typed()
        for changes in ({'schema': 'other'}, {'custody_kind': 'full'}, {'files': 9}, {'bytes': 1}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'receipt_manifest_mismatch'):
                watch.bind_snapshot(dict(receipt, **changes), self.frozen, self.snapshots, {})
        value = json.loads(self.manifest.read_bytes())
        value['runs'][0]['files'] = [r for r in value['runs'][0]['files'] if r['path'] != 'end_context.json']
        value['runs'][0]['bytes'] = sum(r['bytes'] for r in value['runs'][0]['files'])
        receipt.update(files=7, bytes=value['runs'][0]['bytes'])
        self.manifest.write_text(json.dumps(value)); receipt['manifest_sha256'] = watch.file_sha(self.manifest)
        with self.assertRaisesRegex(ValueError, 'receipt_manifest_mismatch'):
            watch.bind_snapshot(receipt, self.frozen, self.snapshots, {})
        value['runs'][0]['omitted'] = [{'path': 'end_context.json', 'reason': 'named_reader_input_absent'}]
        self.manifest.write_text(json.dumps(value)); receipt['manifest_sha256'] = watch.file_sha(self.manifest)
        _, loaded = watch.bind_snapshot(receipt, self.frozen, self.snapshots, {})
        with self.assertRaisesRegex(ValueError, 'not_captured'):
            watch.stage_context('end', loaded)

    def test_succession_preserves_identity_journal_and_never_replays_prior_dispositions(self):
        selected, pins, feed, identity, review, reference = self.succession()
        original_identity = (self.output / '.watch/IDENTITY.json').read_bytes()
        original_index = self.accepted.read_bytes()
        original_journal = (self.output / '.watch/journal.jsonl').read_bytes()
        # The previously missing/error disposition stays final even though the
        # accepted full snapshot now contains both contexts.
        self.pause.touch()
        watcher = watch.Watcher(selected, {}, Path('.'), pins, feed, self.output, self.pause,
            context_runner=lambda *a, **k: self.fail('handled stage replayed'), workers=1,
            input_succession=reference)
        self.run_watcher(watcher)
        self.run_watcher(watch.Watcher(selected, {}, Path('.'), pins, feed, self.output, self.pause,
            context_runner=lambda *a, **k: self.fail('successor restart replayed'), workers=1,
            input_succession=reference))
        self.assertEqual((self.output / '.watch/IDENTITY.json').read_bytes(), original_identity)
        self.assertEqual(self.accepted.read_bytes(), original_index)
        self.assertTrue(watcher.journal.read_bytes().startswith(original_journal))
        self.assertEqual(watcher.handled, {'mi27/fixture/early', 'mi27/fixture/end'})
        self.assertEqual(len(list(watcher.meta.glob('INPUT_SUCCESSION.*.json'))), 1)

    def test_unreviewed_or_branching_successor_fails_without_calls_or_identity_write(self):
        selected, pins, feed, identity, review, reference = self.succession()
        before = (self.output / '.watch/IDENTITY.json').read_bytes()
        self.pause.touch()
        with self.assertRaisesRegex(ValueError, 'reader_queue_identity_changed'):
            watch.Watcher(selected, {}, Path('.'), pins, feed, self.output, self.pause, workers=1).run()
        (self.output / '.watch/INPUT_SUCCESSION.unbound.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'unbound_branch'):
            watch.validate_input_succession(reference, identity, self.output)
        self.assertEqual((self.output / '.watch/IDENTITY.json').read_bytes(), before)

    def test_changed_scientific_pins_selection_or_worker_count_are_refused(self):
        _, _, _, identity, review, _ = self.succession()
        for change in ('prompt', 'selection', 'workers', 'numeric_type', 'arm_checker', 'unlisted'):
            value, expected = copy.deepcopy(review), copy.deepcopy(identity)
            if change == 'prompt':
                expected['pins']['prompts']['rubric'] = 'different rubric'
                value['pin_replacements'].append({'path': ['prompts', 'rubric'], 'old': 'same rubric', 'new': 'different rubric'})
            elif change == 'selection':
                expected['selected_sha256'] = 'other roster'
            elif change == 'workers':
                expected['workers'] = 2
            elif change == 'numeric_type':
                expected['workers'] = True
            elif change == 'arm_checker':
                expected['pins']['per_arm']['first']['reader_config_sha256'] = 'different checker'
            else:
                expected['pins']['reader_config_sha256'] = 'different checker'
            value['new_identity'] = expected
            reference = self.record('changed_' + change + '.json', value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                watch.validate_input_succession(reference, expected, self.output)

    def test_retirement_and_exact_index_journal_prefixes_are_required(self):
        _, _, feed, identity, review, _ = self.succession()
        for key in ('process_absent', 'all_claimed_terminal', 'watch_lock_acquired_nonblocking'):
            retired = json.loads(watch.checked_pin(review['retirement_review'])); retired[key] = False
            changed = dict(review, retirement_review=self.record('bad_' + key + '.json', retired))
            reference = self.record('review_' + key + '.json', changed)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'not_retired'):
                watch.validate_input_succession(reference, identity, self.output)
        reference = self.record('review_original.json', review)
        feed.write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'index_prefix_changed'):
            watch.validate_input_succession(reference, identity, self.output)
        feed.write_bytes(self.accepted.read_bytes())
        (self.output / '.watch/journal.jsonl').write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'journal_prefix_changed'):
            watch.validate_input_succession(reference, identity, self.output)

    def test_successor_runs_only_previously_unclaimed_stage_once(self):
        selected, pins, feed, identity, review, _ = self.succession()
        baseline = Path(review['journal_baseline']['path'])
        raw = b'\n'.join(baseline.read_bytes().splitlines()[:2]) + b'\n'
        baseline.write_bytes(raw)
        (self.output / '.watch/journal.jsonl').write_bytes(raw)
        retired = json.loads(watch.checked_pin(review['retirement_review']))
        retired['journal_baseline'] = self.pin(baseline)
        review['journal_baseline'] = self.pin(baseline)
        review['retirement_review'] = self.record('retirement_early_only.json', retired)
        reference = self.record('succession_early_only.json', review)
        calls = []
        def runner(client, config, prompts, messages, boundary, destination, **kwargs):
            calls.append(destination.name)
            destination.mkdir(parents=True)
            self.pause.touch()
            return {'instruments': {'presence': {'status': 'validated'}}}
        self.run_watcher(watch.Watcher(selected, {}, Path('.'), pins, feed, self.output, self.pause,
            context_runner=runner, workers=1, poll_seconds=0.005, input_succession=reference))
        self.assertEqual(calls, ['end'])
        rows = watch.jsonlines(self.output / '.watch/journal.jsonl', strict=True)
        self.assertEqual(sum(row['event'] == 'claimed' for row in rows), 2)
        self.assertEqual(rows[1]['reader_status'], 'technical_missing')

    def test_native_source_relocation_requires_identical_whole_bytes(self):
        _, _, _, identity, review, _ = self.succession()
        old_ref = self.record('old/native.py', {'source': 'unchanged reader'})
        alias_ref = self.record('new/native.py', {'source': 'unchanged reader'})
        old_identity_path = Path(review['predecessor_identity']['path'])
        old_identity = json.loads(old_identity_path.read_bytes())
        old_identity['pins']['sources'] = {'reader': old_ref}
        old_identity_path.write_text(json.dumps(old_identity))
        review['predecessor_identity'] = self.pin(old_identity_path)
        retired = json.loads(watch.checked_pin(review['retirement_review']))
        retired['predecessor_identity'] = review['predecessor_identity']
        review['retirement_review'] = self.record('retirement_alias.json', retired)
        identity['pins']['sources'] = {'reader': alias_ref}
        review['pin_replacements'].append({'path': ['sources', 'reader'], 'old': old_ref, 'new': alias_ref})
        reference = self.record('succession_alias.json', review)
        watch.validate_input_succession(reference, identity, self.output)
        changed = self.record('changed/native.py', {'source': 'changed reader'})
        identity['pins']['sources']['reader'] = changed
        review['pin_replacements'][-1]['new'] = changed
        reference = self.record('succession_changed_source.json', review)
        with self.assertRaisesRegex(ValueError, 'native_source_changed'):
            watch.validate_input_succession(reference, identity, self.output)


class ContinuousReaderInputTests(TestCase):
    setUp = continuous_tests.SnapshotWatchTests.setUp
    add = continuous_tests.SnapshotWatchTests.add
    transport = continuous_tests.SnapshotWatchTests.transport
    run_watch = continuous_tests.SnapshotWatchTests.run_watch

    def authorize(self, config):
        return dict(continuous_tests.SnapshotWatchTests.authorize(self, config),
                    custody_kind='actor_reader_inputs')

    def prepare_inputs(self):
        self.config = replace(self.config, reader_inputs_only=True)
        for name in ('a', 'b', 'c', 'd'):
            run = self.add(name)
            for filename in snapshot.READER_INPUT_FILES[:4]:
                (run / filename).write_text('{}')
        self.config.destination.mkdir()
        (self.config.destination / 'accepted.jsonl').write_bytes(b'preserved unrelated full index\n')

    def test_continuous_typed_batches_catch_up_using_only_the_typed_index(self):
        self.prepare_inputs()
        result = self.run_watch()
        self.assertEqual(result['reason'], 'selection_complete')
        self.assertEqual(self.calls, ['ssh', 'rsync', 'ssh', 'rsync'])
        self.assertEqual((self.config.destination / 'accepted.jsonl').read_bytes(),
                         b'preserved unrelated full index\n')
        rows = [json.loads(line) for line in (self.config.destination / self.config.index_name).read_bytes().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['custody_kind'] == 'actor_reader_inputs' for row in rows))

    def test_continuous_typed_space_hold_polls_locally_without_changing_reserve(self):
        self.prepare_inputs()
        available, waits = [0], []
        def wait(stop, seconds):
            waits.append(seconds)
            if len(waits) == 2:
                available[0] = 10000
        with patch.object(snapshot.shutil, 'disk_usage', side_effect=lambda _: SimpleNamespace(free=available[0])):
            self.run_watch(wait=wait)
        self.assertEqual(waits, [60, 60])
        held = json.loads((self.directory / '00000001/RESULT.json').read_bytes())
        self.assertFalse(held['remote_call'])
        self.assertEqual(held['hold']['floor_bytes'], 100)
        self.assertEqual(self.calls, ['ssh', 'ssh', 'rsync', 'ssh', 'rsync'])
