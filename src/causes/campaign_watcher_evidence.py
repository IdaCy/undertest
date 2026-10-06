"""Capture a watcher's current ownership and retained handoff evidence.

Only an exclusive evidence directory is written. The capture neither changes
watcher controls nor grants the independent acceptance required by readiness.
"""

from pathlib import Path
import argparse
import hashlib
import json
import os

from causes import campaign_handoff as h
from causes import campaign_watcher_handoff as handoff


def require(value, reason):
    if not value:
        raise ValueError(reason)


def same(left, right):
    return h.encoded(left) == h.encoded(right)


def lineage(configuration, start_result):
    config, result = h.checked(configuration), h.checked(start_result)
    require(config['schema'] == 'causes.campaign_watcher_handoff.v1', 'watcher_configuration_schema')
    for module, key in ((h, 'wrapper'), (handoff, 'implementation')):
        require(h.pin(module.__file__)['sha256'] == config[key]['sha256'], 'watcher_dependency_changed')
        h.checked_bytes(config[key])
    require(result['configuration'] == configuration, 'watcher_start_configuration_changed')
    directory = h.canonical_path(config['directory'])
    source, adjacent = config['successor']['source'], config['successor']['configuration']
    require(result['source'] == source and result['watcher_configuration'] == adjacent
            and directory == Path(source['path']).parent / 'watch'
            and adjacent['path'] == str(Path(source['path']).with_suffix('.config.json')),
            'watcher_successor_scope_changed')
    installed = h.checked(adjacent)
    h.checked_bytes(source)
    require(installed['watcher_sha256'] == source['sha256'] and installed['directory'] == str(directory),
            'watcher_installed_configuration_changed')
    process = h.checked(result['process_receipt'])
    launch = Path(start_result['path']).parent
    require(Path(start_result['path']).name == 'RESULT.json'
            and result['process_receipt']['path'] == str(launch / 'PROCESS.json')
            and same(process, result['process']) and type(process['pid']) is int
            and result['sole_watch_lock_owner'] == process['pid']
            and process['argv'] == [config['python'], source['path']], 'watcher_start_process_changed')
    intent_pin = h.pin(process['intent_path'])
    require(intent_pin['path'] == str(launch / 'INTENT.json')
            and intent_pin['sha256'] == process['intent_sha256'], 'watcher_process_intent_changed')
    intent = h.checked(intent_pin)
    require(intent['action'] == 'release_owned_stop_and_start_one_watcher'
            and intent['configuration'] == configuration and intent['retirement'] == result['retirement']
            and intent['publication_result'] == result['publication_result']
            and intent['source'] == source and intent['watcher_configuration'] == adjacent
            and intent['wrapper'] == config['wrapper'] and intent['argv'] == process['argv'],
            'watcher_start_intent_scope_changed')
    retirement_pin = result['retirement']
    handoff.accepted(intent['retirement_review'], config['reviews']['retirement'], 'retirement', retirement_pin)
    handoff.accepted(intent['publication_review'], config['reviews']['publication'], 'result', result['publication_result'])
    retired, publication = h.checked(retirement_pin), h.checked(result['publication_result'])
    require(retired.get('retired') is True and retired.get('lock_free') is True
            and retired['configuration'] == configuration and same(retired['process'], config['predecessor']['process'])
            and retired['source'] == config['predecessor']['source']
            and publication['proposal'] == retired['proposal']
            and source in publication['current'] and adjacent in publication['current'],
            'watcher_retirement_publication_changed')
    stopped = h.checked(result['owned_stop_retired'])
    ownership = h.checked(retired['stop_ownership'])
    require(result['owned_stop_retired']['path'] == str(launch / 'STOP_RETIRED.json')
            and stopped['ownership'] == retired['stop_ownership'] == intent['owned_stop']
            and stopped['intent'] == intent_pin and ownership['marker'] == retired['owned_stop']
            and ownership['marker']['path'] == str(directory / 'STOP'), 'watcher_stop_lineage_changed')
    archived = h.checked_bytes(ownership['archived_marker'])
    require(len(archived) == ownership['marker']['bytes']
            and hashlib.sha256(archived).hexdigest() == ownership['marker']['sha256'], 'watcher_stop_archive_changed')
    startup = h.checked(result['startup'])
    configuration_hash = hashlib.sha256(json.dumps(installed, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    require(result['startup']['path'] == str(directory / ('START_' + str(process['pid']) + '.json'))
            and startup['pid'] == process['pid'] and startup['script_sha256'] == source['sha256']
            and startup['configuration_sha256'] == configuration_hash
            and startup['interval_seconds'] == installed['interval_seconds'], 'watcher_startup_changed')
    return config, result, intent, retired


def write_bytes(directory, name, raw):
    path = directory / name
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    h.sync_directory(directory)
    return h.pin(path, raw)


def capture(configuration, start_result, output):
    """Return current/successor evidence pins for subsequent independent review."""
    config = h.checked(configuration)
    output = h.canonical_path(output)
    protected = [config['directory'], *config['pause_paths'], configuration['path'],
                 config['predecessor']['source']['path'], config['successor']['source']['path'],
                 config['successor']['configuration']['path'], str(Path(start_result['path']).parent)]
    require(not any(output == Path(path) or Path(path) in output.parents for path in protected),
            'watcher_evidence_destination_overlaps_live_state')
    directory = h.new_record_directory(output)
    try:
        config, result, intent, retired = lineage(configuration, start_result)
        process = result['process']
        watch = h.canonical_path(config['directory'])
        lock, stop = watch / 'watch.lock', watch / 'STOP'
        before_lock = handoff.lock_identity(lock)
        require(list(before_lock) == retired['lock_identity'], 'watcher_lock_inode_changed')
        require(not os.path.lexists(stop), 'watcher_stop_present')
        proc = Path('/proc') / str(process['pid'])
        raw = {'proc_stat_before': (proc / 'stat').read_bytes(),
               'proc_cmdline': (proc / 'cmdline').read_bytes(),
               'proc_locks': Path('/proc/locks').read_bytes(),
               'proc_stat_after': (proc / 'stat').read_bytes()}
        pins = [write_bytes(directory, name, value) for name, value in raw.items()]
        before = raw['proc_stat_before'].decode().rsplit(')', 1)[1].split()
        after = raw['proc_stat_after'].decode().rsplit(')', 1)[1].split()
        argv = [value.decode() for value in raw['proc_cmdline'].split(b'\0') if value]
        require(before[19] == after[19] == str(process['process_starttime_ticks'])
                and before[0] not in ('Z', 'X', 'x') and after[0] not in ('Z', 'X', 'x')
                and argv == process['argv'], 'watcher_live_process_changed')
        owners = []
        for line in raw['proc_locks'].decode().splitlines():
            fields = line.split()
            if len(fields) < 8 or fields[1] == '->':
                continue
            device = fields[5].split(':')
            if len(device) == 3 and (int(device[0], 16), int(device[1], 16), int(device[2])) == (
                    os.major(before_lock[0]), os.minor(before_lock[0]), before_lock[1]):
                owners.append((fields[1], fields[3], int(fields[4])))
        require(owners == [('FLOCK', 'WRITE', process['pid'])], 'watcher_not_sole_lock_owner')
        require(handoff.lock_identity(lock) == before_lock and not os.path.lexists(stop), 'watcher_lock_or_stop_changed')
        # Recheck the immutable inputs and live identity after retaining the raw evidence.
        lineage(configuration, start_result)
        h.process_identity(process)
        require(handoff.lock_identity(lock) == before_lock and h.lock_owned_by(lock, process['pid'])
                and not os.path.lexists(stop), 'watcher_ownership_changed')
        identity = {key: process[key] for key in ('pid', 'process_starttime_ticks', 'argv')}
        current = {'at': h.now(), 'process': identity, 'source': result['source'],
                   'watcher_configuration': result['watcher_configuration'], 'lock_owned': True, 'stop_absent': True,
                   'raw_pins': pins, 'lock_file_identity': dict(zip(('st_dev', 'st_ino'), before_lock)),
                   'start_result': start_result, 'configuration': configuration,
                   'capture_source': h.pin(Path(__file__).absolute()), 'read_only': True}
        current_pin = h.save_new(directory, 'CURRENT.json', current)
        successor = {'at': current['at'], 'process': identity, 'source': result['source'],
                     'process_receipt': result['process_receipt'], 'start_receipt': result['startup'],
                     'prior_retirement': result['retirement'], 'prior_retirement_review': intent['retirement_review'],
                     'stop_retirement': result['owned_stop_retired'], 'lock_owned': True, 'stop_absent': True,
                     'current_observation': current_pin, 'start_result': start_result, 'capture_source': current['capture_source']}
        successor_pin = h.save_new(directory, 'SUCCESSOR_EVIDENCE.json', successor)
        return {'current': current_pin, 'successor': successor_pin}
    except BaseException as error:
        h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
                   'error': str(error)[:500], 'no_runtime_controls_changed': True})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--start-result', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = capture(h.pin(args.configuration.absolute()), h.pin(args.start_result.absolute()), args.output.absolute())
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
