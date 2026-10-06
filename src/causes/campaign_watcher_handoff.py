"""Drain one configured watcher through its owned stop marker and start one successor."""

from pathlib import Path
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time

from causes import campaign_handoff as h


def accepted(reference, flag, key, subject):
    value = h.checked(reference)
    if value.get(flag) is not True or value.get(key) != subject:
        raise ValueError('watcher_review_does_not_bind_subject')
    return value


def lock_identity(path):
    value = h.canonical_path(path).stat()
    return value.st_dev, value.st_ino


def newest_completed_cycle(directory):
    cycles = sorted(path for path in directory.iterdir()
                    if re.fullmatch(r'\d{8}T\d{6}Z_[0-9a-f]{8}', path.name)
                    and path.is_dir() and not path.is_symlink())
    if not cycles:
        raise ValueError('watcher_has_no_cycle')
    result = h.pin(cycles[-1] / 'RESULT.json')
    value = h.checked(result)
    if not isinstance(value.get('checks'), list) or not value.get('ended_at'):
        raise ValueError('newest_watcher_cycle_not_closed')
    return result


class WatcherHandoff:
    def __init__(self, configuration):
        self.reference = configuration
        self.config = h.checked(configuration)
        c = self.config
        if c['schema'] != 'causes.campaign_watcher_handoff.v1':
            raise ValueError('watcher_handoff_configuration_schema')
        if h.pin(Path(__file__).absolute()) != c['implementation'] or h.pin(Path(h.__file__).absolute()) != c['wrapper']:
            raise ValueError('watcher_handoff_source_changed')
        self.directory = h.canonical_path(c['directory'])
        self.lock = self.directory / 'watch.lock'
        self.stop = self.directory / 'STOP'
        self.process = c['predecessor']['process']
        self.before = c['predecessor']['source']
        self.after = c['successor']['source']
        self.adjacent = c['successor']['configuration']
        source = h.canonical_path(self.before['path'])
        if (self.directory != source.parent / 'watch' or self.after['path'] != str(source)
                or self.adjacent['path'] != str(source.with_name(source.stem + '.config.json'))
                or self.process['argv'] != [c['python'], str(source)]
                or not Path(c['python']).is_absolute()):
            raise ValueError('watcher_handoff_scope_changed')
        self.pause_paths = tuple(h.canonical_path(p) for p in c['pause_paths'])
        if not self.pause_paths or self.stop in self.pause_paths:
            raise ValueError('watcher_pause_scope_required')
        self.clear()

    def clear(self):
        h.checked_bytes(self.reference)
        h.checked_bytes(self.config['implementation'])
        h.checked_bytes(self.config['wrapper'])
        if any(os.path.lexists(path) for path in self.pause_paths):
            raise ValueError('pause_blocks_watcher_handoff')

    def proposal(self, reference, review):
        c = self.config
        accepted(review, c['reviews']['candidate'], 'proposal', reference)
        value = h.checked(reference)
        if value.get('watcher_handoff') != self.reference or value.get('requires_watcher_retirement') is not True:
            raise ValueError('proposal_watcher_handoff_changed')
        rows = value['rows']
        targets = [row['target'] for row in rows]
        if len(set(targets)) != len(targets):
            raise ValueError('duplicate_publication_target')
        by_target = {row['target']: row for row in rows}
        for expected in (self.after, self.adjacent):
            row = by_target[expected['path']]
            if {**row['after'], 'path': row['target']} != expected:
                raise ValueError('proposal_watcher_successor_changed')
        if by_target[self.before['path']]['before'] != self.before:
            raise ValueError('proposal_watcher_predecessor_changed')
        return value

    def unchanged_lock(self, expected):
        if lock_identity(self.lock) != expected:
            raise ValueError('watcher_lock_inode_changed')

    def predecessor_alive(self, expected_lock):
        self.clear()
        self.unchanged_lock(expected_lock)
        h.checked_bytes(self.before)
        identity = h.process_identity(self.process)
        if not h.lock_owned_by(self.lock, self.process['pid']):
            raise ValueError('watcher_not_sole_lock_owner')
        return identity

    def predecessor_dead(self, expected_lock):
        self.unchanged_lock(expected_lock)
        state = h.retirement_state(self.process)
        if not (state['absent'] or state.get('state') in ('Z', 'X', 'x')) or not h.lock_free(self.lock):
            raise ValueError('watcher_predecessor_not_retired')
        return state

    def drain(self, proposal, review, directory):
        directory = h.new_record_directory(directory)
        created = False
        observations = []
        try:
            with (self.directory / '.root_handoff.lock').open('a+b') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.proposal(proposal, review)
                original_lock = lock_identity(self.lock)
                before = self.predecessor_alive(original_lock)
                if os.path.lexists(self.stop):
                    raise ValueError('watcher_stop_already_exists')
                startup = h.pin(self.directory / ('START_' + str(self.process['pid']) + '.json'))
                if h.checked(startup)['script_sha256'] != self.before['sha256']:
                    raise ValueError('predecessor_startup_source_changed')
                intent = h.save_new(directory, 'INTENT.json', {
                    'at': h.now(), 'action': 'drain_exact_watcher_through_owned_stop',
                    'configuration': self.reference, 'process': self.process, 'source': self.before,
                    'proposal': proposal, 'proposal_review': review, 'before': before,
                    'lock_identity': list(original_lock), 'no_signals_sent': True})
                self.proposal(proposal, review)
                self.predecessor_alive(original_lock)
                raw = h.encoded({'at': h.now(), 'intent': intent,
                                 'old_process': self.process, 'proposal': proposal}) + b'\n'
                descriptor = os.open(self.stop, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                created = True
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                h.sync_directory(self.directory)
                marker = h.pin(self.stop, raw)
                archive = directory / 'STOP.created'
                with archive.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                owned = h.save_new(directory, 'STOP_OWNERSHIP.json', {
                    'marker': marker, 'stat': h.stat_identity(self.stop),
                    'archived_marker': h.pin(archive, raw), 'intent': intent})
                previous_dead = None
                deadline = time.monotonic() + 1000
                while time.monotonic() < deadline:
                    self.clear()
                    self.unchanged_lock(original_lock)
                    h.verify_owned_barrier(owned, self.stop)
                    state = h.retirement_state(self.process)
                    free = h.lock_free(self.lock)
                    observations.append({'at': h.now(), **state, 'lock_free': free})
                    dead = state.get('state') in ('Z', 'X', 'x')
                    stable = dead and previous_dead == (state.get('state'), state.get('start_ticks'))
                    if free and (state['absent'] or stable):
                        break
                    previous_dead = (state.get('state'), state.get('start_ticks')) if dead else None
                    time.sleep(2)
                else:
                    raise ValueError('watcher_drain_unresolved_preserve_stop')
                self.predecessor_dead(original_lock)
                h.verify_owned_barrier(owned, self.stop)
                final_cycle = newest_completed_cycle(self.directory)
                return h.save_new(directory, 'RETIREMENT.json', {
                    'at': h.now(), 'retired': True, 'lock_free': True, 'source': self.before,
                    'process': self.process, 'configuration': self.reference, 'proposal': proposal,
                    'proposal_review': review, 'lock_identity': list(original_lock),
                    'owned_stop': marker, 'stop_ownership': owned, 'old_startup_receipt': startup,
                    'final_cycle_directory': str(Path(final_cycle['path']).parent),
                    'final_completed_cycle': final_cycle, 'observations': observations,
                    'intent': intent, 'no_signals_sent': True, 'no_actor_controls_changed': True})
        except BaseException as error:
            h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
                       'error': str(error)[:500], 'stop_created': created, 'observations': observations,
                       'no_automatic_retry_or_rollback': True})
            raise

    def start(self, retirement, retirement_review, publication, publication_review, directory):
        directory = h.new_record_directory(directory)
        removed = False
        spawned = False
        try:
            with (self.directory / '.root_handoff.lock').open('a+b') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                c = self.config
                accepted(retirement_review, c['reviews']['retirement'], 'retirement', retirement)
                accepted(publication_review, c['reviews']['publication'], 'result', publication)
                retired = h.checked(retirement)
                published = h.checked(publication)
                if (retired.get('retired') is not True or retired.get('lock_free') is not True
                        or retired['process'] != self.process or retired['source'] != self.before
                        or retired['configuration'] != self.reference
                        or published['proposal'] != retired['proposal']):
                    raise ValueError('watcher_retirement_publication_join_changed')
                proposal = self.proposal(retired['proposal'], retired['proposal_review'])
                expected = [{**row['after'], 'path': row['target']} for row in proposal['rows']]
                expected += [row['pin'] for row in proposal.get('preserved', [])]
                current = published['current']
                if (len(current) != len(expected) or len({r['path'] for r in current}) != len(current)
                        or sorted(current, key=lambda r:r['path']) != sorted(expected, key=lambda r:r['path'])):
                    raise ValueError('published_watcher_targets_changed')
                original_lock = tuple(retired['lock_identity'])
                ownership = retired['stop_ownership']
                if h.checked(ownership)['marker'] != retired['owned_stop']:
                    raise ValueError('retirement_stop_binding_changed')
                def validate():
                    self.clear()
                    self.predecessor_dead(original_lock)
                    h.verify_owned_barrier(ownership, self.stop)
                    for reference in current:
                        h.checked_bytes(reference)
                validate()
                new_config = h.checked(self.adjacent)
                if (new_config['watcher_sha256'] != self.after['sha256']
                        or new_config['directory'] != str(self.directory)):
                    raise ValueError('successor_watcher_configuration_changed')
                intent = h.save_new(directory, 'INTENT.json', {
                    'at': h.now(), 'action': 'release_owned_stop_and_start_one_watcher',
                    'configuration': self.reference, 'retirement': retirement,
                    'retirement_review': retirement_review, 'publication_result': publication,
                    'publication_review': publication_review, 'owned_stop': ownership,
                    'source': self.after, 'watcher_configuration': self.adjacent,
                    'wrapper': c['wrapper'], 'argv': self.process['argv'], 'no_automatic_restart': True})
                validate()
                with self.lock.open('rb') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.clear()
                    self.unchanged_lock(original_lock)
                    h.verify_owned_barrier(ownership, self.stop)
                    self.stop.unlink()
                    removed = True
                    h.sync_directory(self.directory)
                    retired_stop = h.save_new(directory, 'STOP_RETIRED.json', {
                        'at': h.now(), 'ownership': ownership, 'intent': intent})
                self.clear()
                if os.path.lexists(self.stop):
                    raise ValueError('watcher_stop_reappeared_no_spawn')
                self.predecessor_dead(original_lock)
                for reference in current:
                    h.checked_bytes(reference)
                argv = [c['python'], c['wrapper']['path'], str(directory / 'PROCESS.json'),
                        json.dumps(self.process['argv']), intent['path'], intent['sha256']]
                with (directory / 'stdout').open('xb') as out, (directory / 'stderr').open('xb') as err:
                    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                             start_new_session=True)
                spawned = True
                h.save_new(directory, 'SPAWN.json', {'at': h.now(), 'pid': child.pid, 'argv': argv, 'intent': intent})
                deadline = time.monotonic() + 15
                startup_path = self.directory / ('START_' + str(child.pid) + '.json')
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise ValueError('watcher_successor_exited_no_retry')
                    if ((directory / 'PROCESS.json').exists() and startup_path.exists()
                            and h.lock_owned_by(self.lock, child.pid)):
                        break
                    time.sleep(.1)
                else:
                    raise ValueError('watcher_successor_start_unresolved_no_retry')
                receipt = h.pin(directory / 'PROCESS.json')
                process = h.checked(receipt)
                if (process['pid'] != child.pid or process['argv'] != self.process['argv']
                        or process['intent_path'] != intent['path'] or process['intent_sha256'] != intent['sha256']):
                    raise ValueError('watcher_successor_process_receipt_changed')
                actual = h.process_identity(process)
                startup = h.pin(startup_path)
                value = h.checked(startup)
                config_hash = hashlib.sha256(json.dumps(new_config, sort_keys=True,
                               separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                if (value['pid'] != child.pid or value['script_sha256'] != self.after['sha256']
                        or value['configuration_sha256'] != config_hash
                        or value['interval_seconds'] != new_config['interval_seconds']):
                    raise ValueError('watcher_successor_startup_changed')
                self.unchanged_lock(original_lock)
                if not h.lock_owned_by(self.lock, child.pid):
                    raise ValueError('watcher_successor_lock_changed')
                return h.save_new(directory, 'RESULT.json', {
                    'at': h.now(), 'configuration': self.reference, 'process': process,
                    'process_receipt': receipt, 'state': actual, 'startup': startup,
                    'source': self.after, 'watcher_configuration': self.adjacent,
                    'sole_watch_lock_owner': child.pid, 'owned_stop_retired': retired_stop,
                    'retirement': retirement, 'publication_result': publication,
                    'ordinary_cycle_and_actor_release_pending': True})
        except BaseException as error:
            h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
                       'error': str(error)[:500], 'owned_stop_removed': removed,
                       'successor_spawned': spawned, 'no_automatic_retry_or_rollback': True})
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    sub = parser.add_subparsers(dest='action', required=True)
    drain = sub.add_parser('drain')
    drain.add_argument('--proposal', type=Path, required=True)
    drain.add_argument('--review', type=Path, required=True)
    start = sub.add_parser('start')
    for name in ('retirement', 'retirement-review', 'publication', 'publication-review'):
        start.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if not args.execute:
        raise ValueError('explicit_watcher_handoff_execution_required')
    handoff = WatcherHandoff(h.pin(args.configuration.absolute()))
    if args.action == 'drain':
        result = handoff.drain(h.pin(args.proposal.absolute()), h.pin(args.review.absolute()), args.output.absolute())
    else:
        result = handoff.start(*(h.pin(getattr(args, name).absolute()) for name in
                                 ('retirement', 'retirement_review', 'publication', 'publication_review')),
                               args.output.absolute())
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
