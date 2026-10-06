from pathlib import Path, PurePosixPath
import argparse
import datetime as dt
import hashlib
import json
import os
import socket
import stat


FIELDS = ('run_id', 'seed', 'block_seed', 'task_seed', 'model_seed')
SEED_MODULUS = 2**31 - 1
CAPTURE_SCHEMA = 'causes.allocation_metadata_census.v1'
CONFIG_SCHEMA = 'causes.allocation_census_config.v1'


def strict_json(raw):
    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out:
                raise ValueError('duplicate_json_key')
            out[key] = value
        return out
    def constant(value):
        raise ValueError('nonfinite_json_number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode()


def pin(path, raw):
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def relative(value):
    if type(value) is not str or not value or '\x00' in value:
        raise ValueError('invalid_relative_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(x in ('.', '..') for x in path.parts):
        raise ValueError('noncanonical_relative_path')
    return path


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('noncanonical_or_symlink_path')
    return path


def identity(value):
    if type(value) is not dict:
        raise ValueError('allocation_object_required')
    rid = value.get('run_id')
    if type(rid) is not str or not rid or len(PurePosixPath(rid).parts) != 1 or relative(rid).name != rid:
        raise ValueError('invalid_allocation_run_id')
    out = {'run_id': rid}
    for key in FIELDS[1:]:
        seed = value.get(key)
        if seed is not None and (type(seed) is not int or not 0 <= seed < SEED_MODULUS):
            raise ValueError('invalid_typed_allocation_seed')
        out[key] = seed
    return out


def validate_host(config):
    if type(config) is not dict or type(config.get('host')) is not str or not config['host']:
        raise ValueError('explicit_host_required')
    base = PurePosixPath(config['base'])
    if not base.is_absolute() or str(base) != config['base'] or '..' in base.parts:
        raise ValueError('noncanonical_base')
    for key in ('roots', 'top_jobs', 'control_directories'):
        values = config[key]
        if type(values) is not list or len(set(values)) != len(values):
            raise ValueError('distinct_inventory_required')
        for value in values:
            relative(value)
    if not config['roots']:
        raise ValueError('empty_root_inventory')
    for root in config['roots']:
        parts = relative(root).parts
        if not (len(parts) == 2 and parts[0] == 'pilots' or len(parts) == 3 and parts[0] == 'runs'):
            raise ValueError('unsupported_inventory_depth')
    if any(len(relative(p).parts) != 1 or not Path(p).match('*jobs*.json') for p in config['top_jobs']):
        raise ValueError('invalid_top_job_inventory')
    if any(str(relative(p).parent) not in config['roots'] or relative(p).name == '.dispatcher' for p in config['control_directories']):
        raise ValueError('invalid_control_exception')
    for name in ('max_file_bytes', 'max_total_bytes', 'max_files', 'max_directory_entries', 'max_allocations'):
        if type(config['limits'].get(name)) is not int or config['limits'][name] <= 0:
            raise ValueError('positive_census_limits_required')
    return config


def _bounded_read(path, limit):
    path = canonical(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError('census_file_type_or_size')
    keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or any(getattr(before, k) != getattr(opened, k) for k in keys):
            raise ValueError('census_file_replaced_before_open')
        raw = stream.read(limit + 1)
        finished = os.fstat(stream.fileno())
        if any(getattr(opened, k) != getattr(finished, k) for k in keys):
            raise ValueError('census_file_changed_during_read')
    after = path.lstat()
    if len(raw) > limit or any(getattr(before, k) != getattr(after, k) for k in keys):
        raise ValueError('census_file_changed_or_oversized')
    return raw


def collect_host(config):
    validate_host(config)
    base = canonical(config['base'])
    limits = config['limits']
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    def entries(path):
        path = canonical(path)
        if not path.exists():
            return []
        rows = []
        for child in path.iterdir():
            if child.is_symlink():
                raise ValueError('symlink_inventory_entry')
            rows.append(child)
            if len(rows) > limits['max_directory_entries']:
                raise ValueError('directory_entry_bound')
        return sorted(rows)
    roots = [p for p in entries(base/'pilots') if p.is_dir()]
    roots += [p for family in entries(base/'runs') if family.is_dir() for p in entries(family) if p.is_dir()]
    if {str(p.relative_to(base)) for p in roots} != set(config['roots']):
        raise ValueError('unreviewed_run_root_inventory')
    top = [p for p in entries(base) if p.match('*jobs*.json')]
    if {p.name for p in top} != set(config['top_jobs']):
        raise ValueError('unreviewed_pilot_job_inventory')
    files, missing, controls = [], [], []
    total_bytes = allocation_count = 0
    def read(path, kind, root=None):
        nonlocal total_bytes, allocation_count
        if len(files) >= limits['max_files']:
            raise ValueError('metadata_file_bound')
        raw = _bounded_read(path, min(limits['max_file_bytes'], limits['max_total_bytes'] - total_bytes))
        total_bytes += len(raw)
        data = strict_json(raw)
        values = ([job['allocation'] for job in data['jobs']] if kind == 'persisted_extension_block' else
                  [job['allocation'] for job in data] if kind == 'retained_pilot_jobs' else [data])
        values = [identity(value) for value in values]
        allocation_count += len(values)
        if allocation_count > limits['max_allocations']:
            raise ValueError('allocation_count_bound')
        if kind == 'actual_allocation' and values[0]['run_id'] != path.parent.name:
            raise ValueError('allocation_basename_mismatch')
        files.append({**pin(path, raw), 'kind': kind, 'root': root, 'allocations': values})
    for path in top:
        read(path, 'retained_pilot_jobs')
    for root in sorted(roots):
        rel = str(root.relative_to(base))
        # As in the original collector, retain the directory cut before reads.
        attempts = []
        for path in entries(root):
            if not path.is_dir() or path.name == '.dispatcher':
                continue
            if str(path.relative_to(base)) in config['control_directories']:
                controls.append({'path': str(path), 'entry_names': [p.name for p in entries(path)]})
            else:
                attempts.append(path)
        for attempt in attempts:
            path = attempt/'allocation.json'
            if path.exists() or path.is_symlink():
                read(path, 'actual_allocation', rel)
            else:
                missing.append({'path': str(attempt), 'run_id': attempt.name, 'root': rel})
        for path in entries(root/'.dispatcher/blocks'):
            if path.match('[0-9]*.json'):
                read(path, 'persisted_extension_block', rel)
    return {'schema': CAPTURE_SCHEMA, 'host': config['host'], 'hostname': socket.gethostname(),
            'started_at': started, 'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'host_config_sha256': hashlib.sha256(encoded(config)).hexdigest(),
            'roots': sorted(str(p) for p in roots), 'top_job_files': sorted(str(p) for p in top),
            'files': files, 'attempt_directories_without_allocation': missing,
            'control_directories': controls, 'metadata_bytes_read': total_bytes,
            'reads_actor_or_probe_outcomes': False}


def _selected(value, selector):
    for key in selector:
        value = value[key]
    if type(value) is not list:
        raise ValueError('reservation_rows_required')
    return value


def compose_reservation(config, captures, frozen_inputs):
    if config.get('schema') != CONFIG_SCHEMA:
        raise ValueError('census_config_schema')
    hosts = config['hosts']
    if len({h['host'] for h in hosts}) != len(hosts) or not hosts:
        raise ValueError('distinct_configured_hosts_required')
    if set(captures) != {h['host'] for h in hosts} or len(frozen_inputs) != len(config['frozen_inputs']):
        raise ValueError('exact_census_inputs_required')
    by_id, locations, provenance = {}, {}, []
    def reserve(value):
        row = identity(value); rid = row['run_id']
        if rid in by_id and encoded(by_id[rid]) != encoded(row):
            raise ValueError('allocation_identity_conflict')
        by_id[rid] = row
        return rid
    for spec, item in zip(config['frozen_inputs'], frozen_inputs, strict=True):
        if spec['kind'] not in ('jobs', 'allocations') or type(spec['expected_count']) is not int or spec['expected_count'] < 0:
            raise ValueError('frozen_reservation_schema')
        for location in spec.get('allowed_locations', []):
            if type(location) is not list or len(location) != 2 or not any(location[0] == h['host'] and location[1] in h['roots'] for h in hosts):
                raise ValueError('frozen_location_not_registered')
        if item['pin'] != spec['pin']:
            raise ValueError('frozen_input_pin_mismatch')
        values = _selected(item['value'], spec['selector'])
        if len(values) != spec['expected_count']:
            raise ValueError('frozen_reservation_count')
        for value in values:
            rid = reserve(value['allocation'] if spec['kind'] == 'jobs' else value)
            if spec.get('allowed_locations'):
                locations.setdefault(rid, set()).update(tuple(x) for x in spec['allowed_locations'])
        provenance.append(spec['pin'])
    cuts, capture_pins, blocks = [], [], []
    pending_actual = []
    for host in hosts:
        validate_host(host)
        item = captures[host['host']];data = item['value'];base = PurePosixPath(host['base'])
        if (data.get('schema') != CAPTURE_SCHEMA or data.get('host') != host['host'] or
                data.get('host_config_sha256') != hashlib.sha256(encoded(host)).hexdigest() or
                data.get('reads_actor_or_probe_outcomes') is not False):
            raise ValueError('capture_scope_mismatch')
        if data['collector_source']['sha256'] != config['collector_sha256']:
            raise ValueError('collector_source_mismatch')
        if data['roots'] != sorted(str(base/r) for r in host['roots']) or data['top_job_files'] != sorted(str(base/p) for p in host['top_jobs']):
            raise ValueError('capture_inventory_mismatch')
        start, end = (dt.datetime.fromisoformat(data[k]) for k in ('started_at', 'finished_at'))
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError('invalid_capture_bounds')
        paths = set()
        if len(data['files']) > host['limits']['max_files'] or sum(f['bytes'] for f in data['files']) > host['limits']['max_total_bytes']:
            raise ValueError('capture_read_bounds')
        for f in data['files']:
            path = PurePosixPath(f['path'])
            if f['path'] in paths or not path.is_absolute() or str(path) != f['path'] or '..' in path.parts:
                raise ValueError('duplicate_or_noncanonical_capture_file')
            paths.add(f['path'])
            kind, root = f['kind'], f['root']
            if kind == 'retained_pilot_jobs':
                if root is not None or f['path'] not in data['top_job_files']:
                    raise ValueError('pilot_file_scope')
            elif root not in host['roots']:
                raise ValueError('allocation_root_scope')
            elif kind == 'actual_allocation':
                if path.name != 'allocation.json' or path.parent.parent != base/root or len(f['allocations']) != 1:
                    raise ValueError('actual_allocation_path_scope')
            elif kind == 'persisted_extension_block':
                if path.parent != base/root/'.dispatcher/blocks' or not path.match('[0-9]*.json'):
                    raise ValueError('persisted_block_path_scope')
                blocks.append({'host': host['host'], 'file': {k: f[k] for k in ('path','bytes','sha256')}, 'allocation_count': len(f['allocations'])})
            else:
                raise ValueError('unreviewed_metadata_kind')
            for allocation in f['allocations']:
                rid = reserve(allocation)
                if kind == 'actual_allocation':
                    if rid != path.parent.name:
                        raise ValueError('allocation_basename_mismatch')
                    pending_actual.append((rid, host['host'], root))
        if data['attempt_directories_without_allocation']:
            raise ValueError('attempt_without_allocation_requires_reconciliation')
        if {x['path'] for x in data['control_directories']} != {str(base/p) for p in host['control_directories']}:
            raise ValueError('control_exception_inventory_mismatch')
        cuts.append({'host': host['host'], 'hostname': data['hostname'], 'started_at': data['started_at'], 'finished_at': data['finished_at']})
        capture_pins.append(item['pin'])
    for rid, host, root in pending_actual:
        if rid in locations and (host, root) not in locations[rid]:
            raise ValueError('frozen_allocation_wrong_host_or_root')
    seeds = sorted({row[k] for row in by_id.values() for k in FIELDS[1:] if type(row[k]) is int and row[k] > 0})
    return {'schema': 'causes.corpus_allocation_reservation.v1', 'run_ids': sorted(by_id),
            'allocation_seeds': seeds, 'allocation_identities': [by_id[k] for k in sorted(by_id)],
            'host_cuts': cuts, 'capture_pins': capture_pins, 'frozen_input_pins': provenance,
            'persisted_full_blocks': blocks, 'root_admission_authorized': False,
            'limits': 'Non-atomic, dated allocation metadata only. Full frozen jobs and persisted blocks reserve unissued identities. Later allocations on either host require a new cut. Numeric allocation seeds only; no future or derived request-seed uniqueness, provider, history, closure, resource or admission claim.'}


def _load_pin(record, limit):
    raw = _bounded_read(record['path'], limit)
    if pin(record['path'], raw) != record:
        raise ValueError('pinned_input_changed')
    return strict_json(raw)


def save_exclusive(path, value):
    path = canonical(path)
    raw = encoded(value)
    with path.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return pin(path, raw)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('collect', 'compose'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--host')
    parser.add_argument('--captures', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    raw = _bounded_read(args.config, 8*1024**2);config = strict_json(raw)
    source = Path(__file__).resolve();source_pin = pin(source, source.read_bytes())
    if config.get('schema') != CONFIG_SCHEMA or config['collector_sha256'] != source_pin['sha256']:
        raise ValueError('pinned_census_source_required')
    if args.action == 'collect':
        if args.captures is not None:
            raise ValueError('collect_capture_inputs_unexpected')
        host = next((h for h in config['hosts'] if h['host'] == args.host), None)
        if host is None:
            raise ValueError('configured_host_required')
        value = collect_host(host);value['collector_source'] = source_pin
    else:
        if args.host is not None or args.captures is None:
            raise ValueError('compose_capture_map_required')
        capture_map_raw = _bounded_read(args.captures, 8*1024**2)
        records = strict_json(capture_map_raw)
        captures = {host: {'pin': p, 'value': _load_pin(p, config['max_capture_bytes'])} for host, p in records.items()}
        frozen = [{'pin': spec['pin'], 'value': _load_pin(spec['pin'], config['max_frozen_bytes'])} for spec in config['frozen_inputs']]
        value = compose_reservation(config, captures, frozen)
        value['capture_map'] = pin(args.captures, capture_map_raw)
    value['configuration'] = pin(args.config, raw);value['composition_source'] = source_pin
    print(json.dumps(save_exclusive(args.output, value)))


if __name__ == '__main__':
    main()
