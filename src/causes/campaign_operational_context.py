"""Retain one ordinary metadata context, including a failed companion observation."""
from pathlib import Path, PurePosixPath
import datetime as dt
import hashlib
import json
import sys

JP_FIELDS = ('at', 'controller', 'lock_owner', 'provider', 'journal', 'gpu',
             'metrics', 'valid', 'prior_local_owners_closed')


def pin(path, raw=None):
    path = Path(path)
    raw = path.read_bytes() if raw is None else raw
    return {'path': str(path), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def save(cut, name, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, indent=2) + '\n').encode()
    path = cut / name
    with path.open('xb') as stream:
        stream.write(raw)
    return pin(path, raw)


def select_latest(watch, cutoff):
    assert cutoff.tzinfo is not None
    rows = []
    for path in watch.glob('*/RESULT.json'):
        raw = path.read_bytes()
        value = json.loads(raw)
        ended = dt.datetime.fromisoformat(value['ended_at'])
        assert ended.tzinfo is not None
        if ended <= cutoff:
            rows.append((ended, str(path), path, raw, value))
    assert rows, 'no_completed_ordinary_cycle'
    selected = max(rows, key=lambda row: (row[0], row[1]))
    return selected[2], selected[3], selected[4], len(rows)


def companion_projection(companion, prior_qualified_failures, companion_host):
    assert isinstance(companion, dict), 'malformed_companion_projection'
    hosts = companion.get('hosts')
    matches = ([row.get('observation') for row in hosts
                if isinstance(row, dict) and row.get('host') == companion_host]
               if isinstance(hosts, list) else [])
    # An invalid companion must not borrow a partial or previous healthy JP body.
    candidate = matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else None
    journal = candidate.get('journal') if candidate else None
    events = journal.get('event_counts') if isinstance(journal, dict) else None
    usable = (companion.get('valid') is True and candidate is not None
              and all(key in candidate for key in JP_FIELDS)
              and candidate.get('valid') is True
              and candidate.get('prior_local_owners_closed') is True
              and isinstance(candidate.get('at'), str)
              and all(isinstance(candidate.get(key), dict)
                      for key in ('controller', 'provider', 'gpu'))
              and all(isinstance(candidate.get(key), list)
                      for key in ('lock_owner', 'metrics'))
              and isinstance(events, dict)
              and all(isinstance(key, str) and type(value) is int and value >= 0
                      for key, value in events.items())
              and type(events.get('route_failure')) is int)
    jp = {key: candidate[key] for key in JP_FIELDS} if usable else None
    failures = events['route_failure'] if usable else None
    return {
        'companion_metadata': {key: companion[key] for key in
                               ('at', 'started_at', 'required', 'valid', 'error_type', 'alarms')
                               if key in companion},
        'jp_current_context_available': usable,
        'jp': jp,
        'jp_reporting': {
            'previous_qualified_aggregate_route_failures': prior_qualified_failures,
            'current_raw_aggregate_route_failures': failures,
            'binding_status': 'pending' if usable else 'unavailable',
            'new_event_case_identity_accepted': False,
            'case_identity_inferred': False,
            'qualified_context_producer_executed': False,
        },
        'qualified_JP_context_accepted': False,
    }


def source_path(stdout, campaign):
    lines = stdout.read_bytes().splitlines()
    assert lines, 'ordinary_check_did_not_retain_a_source_path'
    path = Path(lines[0].decode())
    assert path.is_absolute() and path.parent == campaign / 'hosts' and path.suffix == '.json'
    assert path.resolve() == path and not path.is_symlink()
    return path


def checked(reference):
    path = Path(reference['path'])
    assert pin(path) == reference, 'context_input_pin_changed'
    return json.loads(path.read_bytes())


def context_binding(preparation, review):
    preparation, review = map(Path, (preparation, review))
    binding = json.loads(preparation.read_bytes())
    acceptance = json.loads(review.read_bytes())
    assert acceptance.get('accepted_operational_context_binding') is True
    assert acceptance['binding'] == pin(preparation), 'context_binding_review_changed'
    assert binding['schema'] == 'causes.operational_context_binding.v1'
    assert binding['source'] == pin(Path(__file__).resolve()), 'context_source_changed'
    campaign = Path(binding['campaign_root'])
    assert campaign.is_absolute() and campaign.resolve() == campaign
    assert not any(p.is_symlink() for p in (campaign, *campaign.parents))
    assert campaign in preparation.resolve().parents and campaign in review.resolve().parents
    registry = checked(binding['registry'])
    assert registry['schema'] == 'causes.output_root_registry.v1'
    roots = binding['physical_group_roots']
    assert roots and len(roots) == len(set(roots)) and set(roots) <= set(registry['sync_roots'])
    for root in roots:
        path = PurePosixPath(root)
        assert not path.is_absolute() and str(path) == root and '..' not in path.parts
    entries = binding['status_entries']
    assert isinstance(entries, dict) and entries
    for name, entry in entries.items():
        assert Path(name).name == name and name.endswith('.py')
        source = campaign / 'hosts' / name
        assert source.resolve() == source and not source.is_symlink()
        assert entry['source'] == pin(source), 'context_status_source_changed'
        assert entry['config']['path'] == str(source.with_suffix('.config.json'))
        config = checked(entry['config'])
        assert config['action'] == 'status'
        assert config['entrypoint_sha256'] == entry['source']['sha256']
    assert binding['phase_inputs'], 'context_phase_evidence_required'
    for reference in binding['phase_inputs'].values():
        checked(reference)
    assert isinstance(binding['companion_host'], str) and binding['companion_host'].strip(), 'context_companion_host_required'
    assert type(binding['previous_qualified_jp_route_failures']) is int
    assert binding['previous_qualified_jp_route_failures'] >= 0
    return campaign, binding


def retain(cut, preparation, review):
    cut, preparation, review = map(Path, (cut, preparation, review))
    campaign, binding = context_binding(preparation, review)
    assert cut.parent == campaign / 'heartbeat' and cut.name.startswith('OPERATIONAL_TICK_')
    assert cut.resolve() == cut and cut.is_dir()
    observation = pin(cut / 'OBSERVATION.json')
    cutoff = dt.datetime.now(dt.timezone.utc)
    selected_at = cutoff.isoformat()
    result_path, result_raw, result, count = select_latest(campaign / 'hosts/watch', cutoff)
    selection = save(cut, 'ORDINARY_SELECTION.v5.json', {
        'selected_at': selected_at,
        'selection': 'Latest RESULT.json whose ended_at is at or before selected_at, ordered by ended_at only; no health filtering or fallback.',
        'completed_results_considered': count,
        'selected_result': pin(result_path, result_raw),
        'selected_result_metadata': result,
    })
    watch = result_path.parent
    selected_status = [row['script'] for row in result['checks']
                       if row['script'] in binding['status_entries']]
    assert len(selected_status) == 1, 'selected_cycle_status_binding_unavailable'
    status_script = selected_status[0]
    status_path = source_path(watch / (status_script + '.stdout'), campaign)
    serving_path = source_path(watch / 'sample_serving_activity.py.stdout', campaign)
    status_raw, serving_raw = status_path.read_bytes(), serving_path.read_bytes()
    status = json.loads(status_raw)
    status_copy = save(cut, 'ORDINARY_STATUS.retained.json', status_raw)
    serving_copy = save(cut, 'ORDINARY_SERVING.retained.json', serving_raw)
    projection = companion_projection(status['companion'], binding['previous_qualified_jp_route_failures'], binding['companion_host'])
    value = {
        'at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'kind': 'retained_raw_ordinary_context_with_explicit_companion_availability',
        'source': pin(Path(__file__).resolve()),
        'observation': observation,
        'context_preparation': pin(preparation),
        'preparation_review': pin(review),
        'status_script': status_script,
        'status_entrypoint': binding['status_entries'][status_script],
        'physical_group_roots': binding['physical_group_roots'],
        'registry_input': binding['registry'],
        'phase_inputs': binding['phase_inputs'],
        'selection_receipt': selection,
        'selected_at': selected_at,
        'selected_result': pin(result_path, result_raw),
        'selected_result_metadata': result,
        'check_streams': [pin(path) for path in sorted(watch.iterdir())
                          if path.name.endswith(('.stdout', '.stderr'))],
        'status_source': pin(status_path, status_raw),
        'status_retained': status_copy,
        'status_at': status['at'],
        'serving_source': pin(serving_path, serving_raw),
        'serving_retained': serving_copy,
        'alarms': status['alarms'],
        'mi_disks': status['disks'],
        'source_identity': status['source_identity'],
        'groups_physical_root_only': [row for row in status['groups'] if row['root'] in
                                     binding['physical_group_roots']],
        **projection,
        'limits': 'Raw metadata only. Invalid or incomplete companion context has no JP projection or inferred current counter. Even a valid raw companion remains unqualified for the pending JP reporting binding. Alarms and failed checks are retained, never filtered; no older healthy fallback, outcome access, remote capture, runtime action or analysis release.',
    }
    return save(cut, 'RETAINED_ORDINARY_CONTEXT.v5.json', value)


if __name__ == '__main__':
    assert len(sys.argv) == 4
    print(json.dumps(retain(*sys.argv[1:]), indent=2))
