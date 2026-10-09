"""Freeze named reader evidence and explicit absences without decoding response or judgment bodies."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from datetime import datetime, timezone


INSTRUMENTS = ('presence', 'strongly_favors', 'leans_towards_or_favors')
STAGE_FILES = ('RESULT.json', 'TECHNICAL_MISSING.json', 'context.json', 'config.json')
INSTRUMENT_FILES = ('request.json', 'schema.json', 'attempt.json', 'receipt.json',
                    'result.json', 'response.raw', 'parsed.json')


def now():
    return datetime.now(timezone.utc).isoformat()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, 'duplicate_metadata_key:' + key)
        result[key] = value
    return result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)


def write(path, value):
    with Path(path).open('xb') as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False).encode() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def metadata(value):
    return {key: getattr(value, 'st_' + key) for key in
            ('dev', 'ino', 'mode', 'size', 'mtime_ns', 'ctime_ns')}


def no_links(path):
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(path), 'noncanonical_path')
    for parent in reversed([path, *path.parents]):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return False
        require(not stat.S_ISLNK(info.st_mode), 'symlink_refused:' + str(parent))
        if parent != path:
            require(stat.S_ISDIR(info.st_mode), 'ancestor_not_directory:' + str(parent))
    return True


def observe(path, destination=None):
    """Hash or exclusively copy opaque bytes; refuse identity or byte-size changes during the read."""
    path = Path(path)
    started_at, started_mono = now(), time.monotonic()
    if not no_links(path):
        return {'path': str(path), 'source': None, 'stat': None,
                'started_at': started_at, 'finished_at': now(),
                'started_monotonic': started_mono, 'finished_monotonic': time.monotonic()}
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), 'not_regular_file:' + str(path))
    h, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    out = None
    try:
        require(metadata(os.fstat(fd)) == metadata(before), 'open_identity_changed:' + str(path))
        if destination is not None:
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            out = destination.open('xb')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            while chunk := stream.read(1024 * 1024):
                h.update(chunk)
                size += len(chunk)
                if out is not None:
                    out.write(chunk)
            require(metadata(os.fstat(stream.fileno())) == metadata(before),
                    'file_changed_during_read:' + str(path))
        require(no_links(path) and metadata(path.lstat()) == metadata(before)
                and size == before.st_size, 'file_changed_after_read:' + str(path))
        if out is not None:
            out.flush()
            os.fsync(out.fileno())
    finally:
        if fd is not None:
            os.close(fd)
        if out is not None:
            out.close()
    source = {'path': str(path), 'bytes': size, 'sha256': h.hexdigest()}
    result = {'path': str(path), 'source': source, 'stat': metadata(before),
              'started_at': started_at, 'finished_at': now(),
              'started_monotonic': started_mono, 'finished_monotonic': time.monotonic()}
    if destination is not None:
        result['copy'] = {**source, 'path': str(destination)}
    return result


def pin(path):
    value = observe(path)['source']
    require(value is not None, 'required_file_absent:' + str(path))
    return value


def checked(reference):
    require(pin(reference['path']) == reference, 'immutable_pin_mismatch:' + reference['path'])


def expected_slots(jobs, model):
    require(safe_name(model), 'invalid_model_key')
    rows, ids = [], set()
    for index, job in enumerate(jobs):
        allocation = job['allocation']
        rid = allocation['run_id']
        require(type(rid) is str and rid not in ('', '.', '..') and Path(rid).name == rid
                and '/' not in rid and rid not in ids, 'invalid_or_duplicate_assignment')
        ids.add(rid)
        stages = allocation['reader_stages']
        require(type(stages) is list and len(stages) == len(set(stages))
                and set(stages) <= {'early', 'end'}, 'invalid_reader_stage_roster')
        for stage in stages:
            rows.append({'job_index': index, 'model': model, 'run_id': rid,
                         'stage': stage, 'frozen_job_sha256': digest(job)})
    return rows


def safe_name(value):
    return type(value) is str and value not in ('', '.', '..') and Path(value).name == value and '/' not in value


def relative_paths(slots):
    paths, seen = set(), set()
    for row in slots:
        require(safe_name(row['model']) and safe_name(row['run_id'])
                and row['stage'] in ('early', 'end'), 'invalid_slot_identity')
        key = (row['model'], row['run_id'], row['stage'])
        require(key not in seen, 'duplicate_slot_identity')
        seen.add(key)
        base = Path(row['model']) / row['run_id']
        paths.add(str(base / 'BINDING.json'))
        stage = base / row['stage']
        paths.update(str(stage / leaf) for leaf in STAGE_FILES)
        paths.update(str(stage / instrument / leaf) for instrument in INSTRUMENTS
                     for leaf in INSTRUMENT_FILES)
    return sorted(paths)


def validate_availability(slots, entries):
    lookup = {row['relative_path']: row for row in entries}
    artifacts = []
    for slot in slots:
        assignment = Path(slot['model']) / slot['run_id']
        stage = assignment / slot['stage']
        refs = {key: lookup[str(path)].get('copy') for key, path in (
            ('binding', assignment / 'BINDING.json'), ('result', stage / 'RESULT.json'),
            ('technical_missing', stage / 'TECHNICAL_MISSING.json'))}
        require(not (refs['result'] and refs['technical_missing']),
                'result_and_technical_missing_conflict:' + str(stage))
        require(not (refs['result'] or refs['technical_missing']) or refs['binding'] is not None,
                'published_stage_without_binding:' + str(stage))
        artifacts.append({**slot, **refs})
    return artifacts


def freeze_reader_inputs(*, source_base, destination, slots, required_pins, provenance):
    """No semantic JSON is loaded from the reader tree; this is custody, not verification."""
    output, base = Path(destination), Path(source_base)
    paths = relative_paths(slots)
    require(not output.is_relative_to(base) and not base.is_relative_to(output),
            'source_and_destination_must_be_disjoint')
    source_pin = pin(__file__)
    for reference in required_pins:
        checked(reference)
    require(no_links(base) and base.is_dir(), 'reader_source_base_invalid')
    require(no_links(output.parent) and not output.exists() and not output.is_symlink(),
            'fresh_exclusive_freeze_required')
    output.mkdir()
    start, mono = now(), time.monotonic()
    entries = []
    try:
        write(output / 'INTENT.json', {'schema': 'causes.reader_artifact_freeze_intent.v1',
              'at': start, 'source': source_pin, 'provenance': provenance,
              'reader_source_base': str(base), 'reader_destination': str(output / 'reader'),
              'semantic_assembly': False, 'model_calls': False, 'retry': False})
        (output / 'reader').mkdir()
        with (output / 'OBSERVED.jsonl').open('xb') as journal:
            for relative in paths:
                entry = observe(base / relative, output / 'reader' / relative)
                entry['relative_path'] = relative
                entries.append(entry)
                journal.write(encoded(entry) + b'\n')
                journal.flush()
                os.fsync(journal.fileno())
        # All selected names, including absences, get a second full opaque check.
        with (output / 'RECHECKED.jsonl').open('xb') as journal:
            for entry in entries:
                current = observe(entry['path'])
                journal.write(encoded(current) + b'\n')
                journal.flush()
                os.fsync(journal.fileno())
                require(current['source'] == entry['source'] and current['stat'] == entry['stat'],
                        'reader_changed_during_freeze:' + entry['relative_path'])
                if entry['source'] is not None:
                    checked(entry['copy'])
        artifacts = validate_availability(slots, entries)
        for reference in required_pins:
            checked(reference)
        checked(source_pin)
        write(output / 'READER_ARTIFACTS.json', artifacts)
        write(output / 'COMPLETE.json', {'schema': 'causes.reader_artifact_freeze_complete.v1',
              'started_at': start, 'finished_at': now(), 'started_monotonic': mono,
              'finished_monotonic': time.monotonic(), 'provenance': provenance,
              'source': source_pin, 'slots': len(slots),
              'named_paths': len(entries), 'present': sum(r['source'] is not None for r in entries),
              'absent': sum(r['source'] is None for r in entries),
              'observations': pin(output / 'OBSERVED.jsonl'),
              'rechecks': pin(output / 'RECHECKED.jsonl'),
              'reader_artifacts': pin(output / 'READER_ARTIFACTS.json'),
              'reader_cut_semantics': 'Separately timed named-file capture with stable before/after bytes and absences; not an atomic filesystem snapshot or the earlier fixed actor cutoff.',
              'raw_verification_performed': False, 'semantic_assembly_performed': False,
              'requires_actual_custody_acceptance_and_reviewed_assembly': True})
    except BaseException as error:
        write(output / 'FAILED.json', {'schema': 'causes.reader_artifact_freeze_failed.v1',
              'started_at': start, 'failed_at': now(), 'source': source_pin,
              'provenance': provenance, 'completed_named_reads': len(entries),
              'error_type': type(error).__name__, 'error': str(error),
              'partial_files_preserved': True, 'automatic_retry': False})
        raise
