"""Validate an explicit endpoint change while retaining frozen input identity."""

import copy
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from causes import campaign_handoff as h


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _accepted(pair, flag, key):
    value, review = h.checked(pair['evidence']), h.checked(pair['review'])
    _require(review.get(flag) is True and review.get(key) == pair['evidence'],
             'unaccepted_storage_input:' + flag)
    return value


def validate(pair):
    transition = _accepted(pair, 'accepted_storage_endpoint_transition', 'transition')
    _require(transition['schema'] == 'causes.storage_endpoint_transition.v1'
             and transition['old_route_requalified'] is False,
             'storage_endpoint_transition_scope_changed')
    original = h.checked(transition['original_config'])
    current = h.checked(transition['successor_config'])
    old_endpoint, new_endpoint = original.get('endpoint'), current.get('endpoint')
    _require(type(old_endpoint) is str and old_endpoint
             and type(new_endpoint) is str and new_endpoint and old_endpoint != new_endpoint
             and h.encoded(dict(current, endpoint=old_endpoint)) == h.encoded(original),
             'storage_endpoint_only_config_change_required')
    route = lambda config: 'model:' + hashlib.sha256(h.encoded([config['model'], config['endpoint']])).hexdigest()
    jobs = h.checked(transition['jobs'])
    ids = [row['allocation']['run_id'] for row in jobs]
    _require(jobs and transition['run_ids'] == ids and len(set(ids)) == len(ids)
             and all(type(rid) is str and rid not in ('', '.', '..') and Path(rid).name == rid for rid in ids)
             and all(row['allocation']['continue_to_end'] is True for row in jobs)
             and transition['old_route'] == route(original) and transition['new_route'] == route(current),
             'storage_endpoint_history_or_destination_changed')
    campaign, output = Path(transition['campaign_root']), Path(transition['output_root'])
    _require(all(path.is_absolute() and str(path) == value and '..' not in path.parts
                 for path, value in ((campaign, transition['campaign_root']), (output, transition['output_root']))),
             'storage_endpoint_output_root_invalid')
    root = str(output.relative_to(campaign))
    _require(root != '.', 'storage_endpoint_output_root_invalid')
    provider = h.checked(transition['provider'])
    endpoint = urlsplit(new_endpoint)
    _require(endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1')
             and type(provider['port']) is int and endpoint.port == provider['port']
             and endpoint.path == '/v1/chat/completions'
             and endpoint.username is None and endpoint.password is None
             and not endpoint.query and not endpoint.fragment,
             'storage_endpoint_provider_address_changed')
    qualification = _accepted(transition['qualification'],
                              'accepted_storage_endpoint_qualification', 'qualification')
    host, backend = transition['host'], transition['backend_label']
    _require(type(host) is str and bool(host.strip()) and provider['host'] == host
             and provider['model'] == current['model']
             and type(backend) is str and bool(backend.strip())
             and qualification['schema'] == 'causes.storage_endpoint_qualification.v1'
             and qualification['accepted'] is True and qualification['host'] == host
             and qualification['provider'] == transition['provider']
             and qualification['backend_label'] == backend
             and qualification['backend_provenance'] == transition['backend_provenance']
             and qualification['config'] == transition['successor_config']
             and qualification['endpoint'] == new_endpoint and qualification['model'] == current['model']
             and type(qualification['qualified_max_full']) is int
             and qualification['qualified_max_full'] in (1, 2, 3),
             'storage_endpoint_provider_qualification_changed')
    h.checked_bytes(transition['backend_provenance'])
    _require(type(qualification['evidence']) is list and qualification['evidence'],
             'storage_endpoint_qualification_evidence_required')
    for reference in qualification['evidence']:
        h.checked_bytes(reference)
    return {'transition': transition, 'original': original, 'current': current,
            'jobs': jobs, 'provider': provider, 'qualification': qualification, 'snapshot_root': root}


def snapshot_config(pair, *, root, job, original_config, original_file_sha256):
    value = validate(pair)
    transition = value['transition']
    _require(root == value['snapshot_root']
             and h.encoded(original_config) == h.encoded(value['original'])
             and original_file_sha256 == transition['original_config']['sha256']
             and sum(h.encoded(job) == h.encoded(row) for row in value['jobs']) == 1,
             'endpoint_snapshot_frozen_scope_mismatch')
    provenance = {'transition': copy.deepcopy(pair), 'host': transition['host'],
                  'provider': transition['provider'], 'backend_label': transition['backend_label'],
                  'backend_provenance': transition['backend_provenance'],
                  'original_config': transition['original_config'],
                  'captured_config': transition['successor_config'],
                  'captured_model_sha256': hashlib.sha256(h.encoded(value['current'])).hexdigest()}
    return value['current'], provenance
