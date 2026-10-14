"""Prepare distinct native probe identities from pinned derived views.

Callers separately establish selection, original-job custody, view derivation
acceptance and runtime authority. This adapter reads saved files; it neither
writes jobs nor authorizes an endpoint, and never changes the native probe.
"""
import copy
import hashlib

from causes import campaign_reader as reader
from causes import campaign_runner as runner
from causes import campaign_supplement as supplement
from causes import derived_reader_inputs as saved
from causes import measurement_view


ROW_SCHEMA = 'causes.derived_probe_input.v1'
PROTOCOL_SCHEMA = 'causes.derived_probe_protocol.v1'
THRESHOLDS = ('presence', 'strongly_favors', 'leans_towards_or_favors')
QUESTION_IDS = tuple('evaluation_' + name for name in THRESHOLDS)
MODULES = dict(campaign_reader=reader, campaign_runner=runner,
               campaign_supplement=supplement, derived_reader_inputs=saved,
               measurement_view=measurement_view)


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _without_endpoint(config):
    require(type(config) is dict and type(config.get('endpoint')) is str
            and config['endpoint'], 'derived_probe_endpoint_required')
    return {key: value for key, value in config.items() if key != 'endpoint'}


def _questions(config):
    require(type(config.get('questions')) is list, 'derived_probe_questions_required')
    result = []
    for question_id, threshold in zip(QUESTION_IDS, THRESHOLDS):
        matches = [q for q in config['questions'] if type(q) is dict and q.get('id') == question_id]
        require(len(matches) == 1, 'derived_probe_question_identity')
        question = matches[0]
        require(question.get('awareness_type') == 'evaluation'
                and question.get('threshold') == threshold
                and type(question.get('text')) is str and question['text'],
                'derived_probe_question_identity')
        result.append(question)
    return result


def _protocol(protocol):
    require(type(protocol) is dict and set(protocol) == {
        'schema', 'family', 'model', 'config', 'sources'}
        and protocol['schema'] == PROTOCOL_SCHEMA, 'derived_probe_protocol_invalid')
    for key in ('family', 'model'):
        supplement.namespace_component(protocol[key])
    require(protocol['family'].startswith('derived_probe_'), 'derived_probe_distinct_family_required')
    require(type(protocol['sources']) is dict and set(protocol['sources']) == set(MODULES),
            'derived_probe_sources_required')
    hashes = {}
    for name, module in MODULES.items():
        ref = protocol['sources'][name]
        saved.raw(ref)
        loaded = saved.pin(module.__file__)
        require(all(loaded[key] == ref[key] for key in ('bytes', 'sha256')),
                'derived_probe_native_source_changed')
        hashes[name] = loaded['sha256']
    require(hashes['campaign_reader'] == reader._SOURCE_SHA256,
            'derived_probe_reader_source_changed')
    config = saved.checked(protocol['config'])
    semantic_config = _without_endpoint(config)
    require(type(config.get('model')) is str and config['model'], 'derived_probe_model_required')
    _questions(config)
    identity = dict(schema=PROTOCOL_SCHEMA, family=protocol['family'], model=protocol['model'],
                    config_sha256=runner.sha(semantic_config), source_sha256=hashes)
    return config, identity


def _assignment(run_id, view_kind, stage):
    require(type(run_id) is str and run_id and type(view_kind) is str and view_kind
            and stage in ('early', 'end'), 'derived_probe_assignment_invalid')


def _provenance(provenance):
    require(type(provenance) is list and provenance, 'derived_probe_provenance_required')
    for ref in provenance:
        saved.raw(ref)


def _origin(origin, run_id, config):
    require(type(origin) is dict and set(origin) == {'allocation', 'config'},
            'derived_probe_origin_required')
    allocation = saved.checked(origin['allocation'])
    original = saved.checked(origin['config'])
    require(type(allocation) is dict and allocation.get('run_id') == run_id
            and type(allocation.get('seed')) is int and allocation['seed'] >= 0
            and type(allocation.get('repetition')) is int and allocation['repetition'] >= 0
            and type(allocation.get('canonical_variant')) is str and allocation['canonical_variant'],
            'derived_probe_original_allocation_changed')
    require(runner.encoded(_without_endpoint(original)) == runner.encoded(_without_endpoint(config)),
            'derived_probe_original_config_changed')
    return {key: allocation[key] for key in ('run_id', 'seed', 'repetition', 'canonical_variant')}


def _payload(config, context, question, seed):
    messages = copy.deepcopy(context) + [dict(role='user', content=question['text'])]
    payload = runner.request(config, messages, seed, config['tools'])
    payload['tool_choice'] = 'none'
    if config.get('probe_response_format'):
        payload['response_format'] = copy.deepcopy(config['probe_response_format'])
    payload['max_tokens'] = config.get('probe_max_tokens', 8192)
    return payload


def _key(run_id, view_kind, stage, view_hash, protocol_hash, original=None):
    return 'derived_probe_' + runner.sha(dict(run_id=run_id, view_kind=view_kind,
        stage=stage, view_sha256=view_hash, protocol_sha256=protocol_hash,
        original=original))


def build_row(run_id, view_kind, stage, reference, provenance, origin, protocol):
    """Bind the original seed/config and all three native request byte hashes.

An execution config may differ from its saved origin only in endpoint. The
endpoint remains pinned in protocol and requires separate runtime approval;
changing it does not invent another scientific measurement identity.
"""
    _assignment(run_id, view_kind, stage)
    config, identity = _protocol(protocol)
    _provenance(provenance)
    original = _origin(origin, run_id, config)
    view = saved.read_view(reference)
    require(view['phase'] == {'early': 'prefix', 'end': 'full'}[stage],
            'derived_probe_stage_changed')
    context = measurement_view.prepare_probe_context(view)
    view_hash = runner.sha(view)
    protocol_hash = runner.sha(identity)
    key = _key(run_id, view_kind, stage, view_hash, protocol_hash, original)
    jobs = []
    for question in _questions(config):
        question_id = question['id']
        seed = runner.seed_for(original['seed'], ['probe', stage, question_id])
        body = runner.encoded(_payload(config, context, question, seed))
        job = dict(family=protocol['family'], model=protocol['model'], run_id=key, stage=stage,
            question_id=question_id, original_run_id=run_id, original_seed=original['seed'], seed=seed,
            context_canonical_sha256=runner.sha(context), question_canonical_sha256=runner.sha(question),
            request_body_sha256=hashlib.sha256(body).hexdigest(), request_body_bytes=len(body),
            request_file_sha256=hashlib.sha256(body + b'\n').hexdigest(),
            derived_view_canonical_sha256=view_hash, protocol_sha256=protocol_hash)
        job['measurement_id'] = '/'.join(job[k] for k in ('family', 'model', 'run_id', 'stage', 'question_id'))
        jobs.append(job)
    final_config, final_identity = _protocol(protocol)
    require(runner.encoded(final_config) == runner.encoded(config) and final_identity == identity,
            'derived_probe_protocol_changed_during_preparation')
    _provenance(provenance)
    require(_origin(origin, run_id, final_config) == original
            and runner.encoded(saved.read_view(reference)) == runner.encoded(view),
            'derived_probe_inputs_changed_during_preparation')
    return dict(schema=ROW_SCHEMA, run_id=run_id, view_kind=view_kind, stage=stage,
        measurement_key=key, disposition='prepared', view=copy.deepcopy(reference),
        provenance=copy.deepcopy(provenance), origin=copy.deepcopy(origin), original=original,
        view_canonical_sha256=view_hash, protocol_sha256=protocol_hash,
        context_canonical_sha256=runner.sha(context), jobs=jobs, runtime_authorized=False)


def unavailable_row(run_id, view_kind, stage, reason, provenance, protocol):
    """Retain a missing assigned arm, with no synthetic context or request."""
    _assignment(run_id, view_kind, stage)
    require(type(reason) is str and reason, 'derived_probe_missing_reason_required')
    _, identity = _protocol(protocol)
    _provenance(provenance)
    protocol_hash = runner.sha(identity)
    return dict(schema=ROW_SCHEMA, run_id=run_id, view_kind=view_kind, stage=stage,
        measurement_key=_key(run_id, view_kind, stage, None, protocol_hash),
        disposition='unavailable', reason=reason, provenance=copy.deepcopy(provenance),
        protocol_sha256=protocol_hash, view=None, jobs=[], runtime_authorized=False)


def _prepare_probe(job, row, protocol, config):
    require(type(job) is dict, 'derived_probe_job_required')
    require(type(row) is dict and row.get('schema') == ROW_SCHEMA
            and row.get('disposition') == 'prepared', 'derived_probe_prepared_row_required')
    require(runner.encoded(config) == runner.encoded(saved.checked(protocol['config'])),
            'derived_probe_runtime_config_changed')
    rebuilt = build_row(row['run_id'], row['view_kind'], row['stage'], row['view'],
                        row['provenance'], row['origin'], protocol)
    require(runner.encoded(rebuilt) == runner.encoded(row), 'derived_probe_row_changed')
    matches = [expected for expected in rebuilt['jobs']
               if expected['measurement_id'] == job.get('measurement_id')]
    require(len(matches) == 1 and runner.encoded(matches[0]) == runner.encoded(job),
            'derived_probe_job_changed')
    question = next(q for q in _questions(config) if q['id'] == job['question_id'])
    context = measurement_view.prepare_probe_context(saved.read_view(row['view']))
    require(runner.sha(context) == job['context_canonical_sha256'], 'derived_probe_context_changed')
    return context, copy.deepcopy(question)


def prepare_probe(job, row, protocol, config):
    """Supply (context, question), or a native integrity hold before admission."""
    try:
        return _prepare_probe(job, row, protocol, config)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise supplement.IntegrityError('derived_probe_source_check:' + str(error)) from error


def validate_membership(assignments, rows, protocol):
    """Verify complete assignment order and explicit missingness without dropping arms."""
    require(type(assignments) is list and assignments and type(rows) is list
            and len(assignments) == len(rows), 'derived_probe_assignment_count_changed')
    seen = set()
    counts = dict(assigned=len(rows), ready=0, unavailable=0, requests=0, missing_slots=0)
    for assignment, row in zip(assignments, rows):
        require(type(assignment) is dict and set(assignment) == {'run_id', 'view_kind', 'stage'}
                and type(row) is dict and all(row.get(k) == v for k, v in assignment.items()),
                'derived_probe_assignment_order_changed')
        identity = tuple(assignment[k] for k in ('run_id', 'view_kind', 'stage'))
        require(identity not in seen, 'derived_probe_duplicate_assignment')
        seen.add(identity)
        if row.get('disposition') == 'prepared':
            rebuilt = build_row(**assignment, reference=row['view'], provenance=row['provenance'],
                                origin=row['origin'], protocol=protocol)
            counts['ready'] += 1
            counts['requests'] += len(QUESTION_IDS)
        else:
            require(row.get('disposition') == 'unavailable', 'derived_probe_disposition_invalid')
            rebuilt = unavailable_row(**assignment, reason=row['reason'],
                                      provenance=row['provenance'], protocol=protocol)
            counts['unavailable'] += 1
            counts['missing_slots'] += len(QUESTION_IDS)
        require(runner.encoded(rebuilt) == runner.encoded(row), 'derived_probe_row_changed')
    require(len({row['measurement_key'] for row in rows}) == len(rows),
            'derived_probe_duplicate_measurement')
    return counts
