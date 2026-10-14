"""Resolve accepted reader indexes without replaying annotation verification.

Callers freeze the descriptor, membership and protocol inputs before use. The
accepted readout peer is the label authority; this module authenticates its
saved joins, not the raw response or parser. No capture, request construction,
model call, directory inventory or write is performed.
"""
import copy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from causes.campaign_analysis import READER_LABELS
from shared.evidence_bytes import read_pinned


THRESHOLDS = ('presence', 'strongly_favors', 'leans_towards_or_favors')
IDENTITY = ('run_id', 'view_kind', 'stage')
REQUEST_FIELDS = (*IDENTITY, 'measurement_key', 'disposition', 'view_canonical_sha256',
                  'protocol_sha256', 'context_canonical_sha256', 'targets_sha256', 'payloads')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _path(value):
    require(type(value) is str and '\0' not in value and value.startswith('/')
            and str(PurePosixPath(value)) == value and '..' not in PurePosixPath(value).parts,
            'literal_absolute_path')
    return PurePosixPath(value)


def _pin(reference):
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}
            and type(reference['bytes']) is int and reference['bytes'] >= 0
            and _hash(reference['sha256']), 'file_pin')
    _path(reference['path'])


def _object(pairs):
    value = {}
    for key, child in pairs:
        require(key not in value, 'duplicate_json_key')
        value[key] = child
    return value


def _float(value):
    number = float(value)
    require(math.isfinite(number), 'nonfinite_json_float')
    return number


class _Reader:
    def __init__(self, maximum_document_bytes, maximum_total_bytes):
        for value in (maximum_document_bytes, maximum_total_bytes):
            require(type(value) is int and 0 < value <= 512*1024**2, 'input_limit')
        self.limit, self.total_limit = maximum_document_bytes, maximum_total_bytes
        self.references, self.values, self.total = {}, {}, 0

    def get(self, reference):
        _pin(reference)
        path = reference['path']
        require(reference['bytes'] <= self.limit, 'document_bound')
        if path in self.references:
            require(self.references[path] == reference, 'conflicting_file_pin')
            return self.values[path]
        require(self.total + reference['bytes'] <= self.total_limit, 'total_input_bound')
        raw = read_pinned(reference, limit=self.limit)
        value = json.loads(raw, object_pairs_hook=_object, parse_float=_float,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
        self.references[path] = copy.deepcopy(reference)
        self.values[path] = value
        self.total += len(raw)
        return value

    def peer(self, reference, flag, **joins):
        value = self.get(reference)
        require(type(value) is dict and value.get(flag) is True and value.get('findings') == [],
                'accepted_peer:' + flag)
        require(all(value.get(k) == v for k, v in joins.items()), 'peer_join:' + flag)
        return value

    def finish(self):
        for reference in self.references.values():
            read_pinned(reference, limit=self.limit)


def _cohort(reader, reference):
    prep = reader.get(reference)
    require(prep.get('schema') == 'causes.frozen_derived_reader_preparation.v1', 'input_schema')
    assignments, ready, missing = [reader.get(prep[k]) for k in ('assignments', 'ready', 'unavailable')]
    require(all(type(x) is list for x in (assignments, ready, missing)), 'input_lists')
    require(all(type(x) is dict and set(x) == set(IDENTITY) for x in assignments), 'assignment_shape')
    require(all(type(x) is dict and all(type(x.get(k)) is str and x[k] for k in IDENTITY)
                and x['stage'] == 'early' for x in assignments + ready + missing), 'early_identity')
    identity = lambda row: tuple(row[k] for k in IDENTITY)
    by_identity = {identity(row): row for row in ready + missing}
    require(len(by_identity) == len(ready) + len(missing) == len(assignments)
            and len(set(map(identity, assignments))) == len(assignments)
            and set(by_identity) == set(map(identity, assignments)), 'input_membership')
    rows = [by_identity[identity(x)] for x in assignments]
    require(ready == [x for x in rows if x.get('disposition') == 'prepared']
            and missing == [x for x in rows if x.get('disposition') == 'unavailable'], 'input_order')
    keys = [x.get('measurement_key') for x in rows]
    require(all(type(k) is str and re.fullmatch('derived_reader_[0-9a-f]{64}', k) for k in keys)
            and len(set(keys)) == len(keys), 'input_keys')
    require(prep['counts'] == dict(assigned=len(rows), ready=len(ready), unavailable=len(missing),
                                  requests=3*len(ready)), 'input_counts')
    return prep, rows, ready


def _custody(reader, descriptor):
    require(type(descriptor) is dict and set(descriptor) ==
            {'preparation', 'input_review', 'closed', 'closed_review'}, 'custody_descriptor')
    prep, rows, ready = _cohort(reader, descriptor['preparation'])
    reader.peer(descriptor['input_review'], 'accepted_frozen_derived_reader_inputs',
                preparation=descriptor['preparation'], source=prep['source'])
    actual = reader.get(descriptor['closed'])
    qualified = {}
    if actual.get('schema') == 'causes.derived_reader_closed_union.v1':
        reader.peer(descriptor['closed_review'], 'accepted_derived_reader_closed_union',
            union=descriptor['closed'], source=actual['source'], binding=actual['binding'],
            binding_review=actual['binding_review'], continuation_actual=actual['continuation_actual'],
            continuation_review=actual['continuation_review'])
        require(actual['inputs'] == descriptor['preparation']
                and actual['inputs_review'] == descriptor['input_review'], 'union_inputs')
        locations = actual['context_locations']
        require(type(locations) is list and all(type(x) is dict and set(x) == {'key', 'root'} for x in locations)
                and [x['key'] for x in locations] == [x['measurement_key'] for x in ready], 'union_locations')
        roots = {x['key']: _path(x['root']) for x in locations}
        key = actual['original_unresolved_key']
        require(key in roots and type(actual['original_missing_instruments']) is list
                and actual['original_missing_instruments']
                and set(actual['original_missing_instruments']) <= set(THRESHOLDS), 'union_missingness')
        qualified[key] = dict(qualification=actual['qualification'],
                             missing=actual['original_missing_instruments'])
    else:
        require(actual.get('schema') == 'causes.finite_reader_route_closed_cut.v1'
                and actual['prepared'] == descriptor['preparation'], 'closed_schema')
        peer = reader.peer(descriptor['closed_review'], 'accepted_derived_reader_batch_closed_cut',
            actual=descriptor['closed'], prepared=descriptor['preparation'], root=actual['root'],
            plan=actual['plan'], journal=actual['journal'])
        require(peer.get('accepted_finite_reader_route_closed_cut') is True
                and peer.get('cohort_kind') == 'derived', 'native_closed_peer')
        require(actual.get('all_claims_accounted') is True and actual['active'] == actual['unclaimed'] == 0
                and actual['ready'] == actual['claimed'] == actual['finished_or_unresolved'] == len(ready)
                and len(actual['claimed_keys']) == len(set(actual['claimed_keys'])) == len(ready)
                and set(actual['claimed_keys']) == {x['measurement_key'] for x in ready}, 'closed_membership')
        roots = {x['measurement_key']: _path(actual['root']) for x in ready}
    return prep, {x['measurement_key']: x for x in rows}, roots, qualified


def _protocol(reader, prep, row, protocols):
    native = prep['protocol']
    require(native['schema'] == 'causes.derived_reader_protocol.v1', 'protocol_schema')
    config = reader.get(native['checker_config'])
    for name in THRESHOLDS:
        p = protocols[name]
        require(type(p) is dict and set(p) == {'schema', 'reader_source_sha256', 'config_sha256',
            'rubric_sha256', 'threshold_sha256', 'schema_sha256', 'instrument', 'phase', 'awareness_type'}
            and p['schema'] == 'causes.saved_reader_instrument_protocol.v1' and p['instrument'] == name
            and p['phase'] == 'prefix' and p['awareness_type'] == 'evaluation'
            and all(_hash(p[k]) for k in p if k.endswith('_sha256')), 'semantic_protocol')
        require(p['reader_source_sha256'] == native['reader_source']['sha256']
                and p['config_sha256'] == digest(config)
                and p['rubric_sha256'] == native['prompts']['rubric']['sha256']
                and p['threshold_sha256'] == native['prompts'][name]['sha256'], 'protocol_identity')
        if row['disposition'] == 'prepared':
            payload = row['payloads'][name]
            require(type(payload) is dict and set(payload) ==
                    {'canonical_sha256', 'body_bytes', 'file_sha256', 'pins'}
                    and _hash(payload['canonical_sha256']) and _hash(payload['file_sha256'])
                    and type(payload['body_bytes']) is int and payload['body_bytes'] > 0,
                    'payload_identity')
            require(payload['pins'] == {k: p[k] for k in
                ('config_sha256', 'rubric_sha256', 'threshold_sha256', 'schema_sha256')}, 'payload_protocol')


def resolve_saved_readers(descriptors, *, membership, membership_review, protocols,
                          maximum_document_bytes=4*1024**2, maximum_total_bytes=64*1024**2,
                          maximum_result_bytes=12*1024**2):
    """Return authenticated neutral reader measurements and frozen membership.

    Descriptors, in frozen comparison order, have variant/readout/review pins.
    Membership uses the accepted frozen_neutral_matched_member_mapping schema.
    Protocols maps the three native threshold names to saved_reader_instrument_
    protocol descriptors (prepared context, prefix phase, evaluation type).

    Returned measurements satisfy the reader portion of neutral_four_cell's
    registry. Origins carry physical pins and aliases; comparisons are retained
    verbatim, including unsupported members. Actual response files are not read.
    An absent/corrupt saved join raises rather than creating a missing label.
    """
    require(type(descriptors) is list and descriptors and type(protocols) is dict
            and set(protocols) == set(THRESHOLDS), 'resolver_inputs')
    require(type(maximum_result_bytes) is int and 0 < maximum_result_bytes <= 512*1024**2, 'result_limit')
    rd = _Reader(maximum_document_bytes, maximum_total_bytes)
    frozen = rd.get(membership)
    rd.peer(membership_review, 'accepted_frozen_neutral_matched_membership', actual=membership)
    require(frozen.get('schema') == 'causes.frozen_neutral_matched_member_mapping.v1'
            and type(frozen.get('comparisons')) is list, 'membership_schema')
    comparisons = frozen['comparisons']
    require(all(type(x) is dict and set(x) == {'variant', 'readout', 'review'} for x in descriptors)
            and [x['variant'] for x in descriptors] == [x['variant'] for x in comparisons]
            and len({x['variant'] for x in descriptors}) == len(descriptors), 'comparison_order')
    measurements, origins, keys, physical_paths, consumed = {}, {}, {}, {}, []
    payload_protocol_seen = False
    for descriptor, comparison in zip(descriptors, comparisons):
        out = rd.get(descriptor['readout'])
        peer = rd.peer(descriptor['review'], 'accepted_saved_derived_reader_readout', result=descriptor['readout'])
        require(out.get('schema') in ('causes.derived_reader_readout.v1', 'causes.derived_reader_composite_readout.v1')
                and out.get('inference_released') is False and out.get('direct_self_report_included') is False,
                'saved_readout_schema')
        for field in ('source', 'source_review', 'modules', 'specification', 'specification_review',
                      'preparation', 'input_review', 'counts'):
            require(peer.get(field) == out.get(field), 'readout_peer_join:' + field)
        prep, rows, ready = _cohort(rd, out['preparation'])
        rd.peer(out['input_review'], 'accepted_frozen_derived_reader_inputs',
                preparation=out['preparation'], source=prep['source'])
        spec = rd.get(out['specification'])
        rd.peer(out['specification_review'], 'accepted_frozen_derived_reader_readout_specification',
                specification=out['specification'], preparation=out['preparation'])
        require(spec.get('schema') == 'causes.frozen_derived_reader_readout_specification.v1'
                and spec.get('awareness_type') == 'evaluation' and spec.get('thresholds') == list(THRESHOLDS)
                and spec.get('measurement_type') == 'independent_verbalized_awareness_reader'
                and spec.get('inference_released') is False, 'reader_instrument_scope')
        require(comparison['reader_specification'] == out['specification']
                and comparison['reader_pairs'] == prep['pairs'] and comparison['stage'] == 'early'
                and all(spec[k] == prep[k] for k in ('ready', 'unavailable', 'pairs')),
                'frozen_comparison_inputs')
        require(out['modules']['campaign_reader']['sha256'] == prep['protocol']['reader_source']['sha256'],
                'native_reader_source')
        pairs = rd.get(prep['pairs'])
        require(type(pairs) is list and len({(x['task_id'], x['draw_key']) for x in pairs}) == len(pairs),
                'unique_task_draw_pairs')
        included = [x for x in comparison['members'] if x['disposition'] == 'included']
        unsupported = [x for x in comparison['members'] if x['disposition'] == 'unsupported']
        require(len(included) + len(unsupported) == comparison['fixed_pairs'] == len(comparison['members'])
                and len(included) == comparison['included_pairs'] == len(pairs)
                and all(type(x.get('reason')) is str and x['reason'] for x in unsupported), 'full_fixed_membership')
        by_key = {x['measurement_key']: x for x in rows}; used = []
        for member, pair in zip(included, pairs):
            require((member['task_id'], member['draw_key']) == (pair['task_id'], pair['draw_key'])
                    and set(member['arms']) == set(pair['arms']) == {'baseline', 'treatment'}, 'pair_membership')
            for arm in ('baseline', 'treatment'):
                selected, assigned = member['arms'][arm], pair['arms'][arm]
                key = selected['reader_key']; require(key in by_key, 'reader_key_missing')
                row = by_key[key]; used.append(key)
                require(assigned['measurement_key'] == key and assigned['run_id'] == row['run_id']
                        == selected['original_run_id'] and assigned['disposition'] == row['disposition']
                        == selected['input_disposition'] and row['view_kind'] == selected['neutral_view'], 'arm_identity')
        require(len(used) == len(set(used)) == len(rows) and set(used) == set(by_key), 'full_row_membership')
        require(out['counts'] == spec['counts'] == dict(assigned_arms=len(rows), pairs=len(pairs),
                ready_arms=len(ready), tasks=len({x['task_id'] for x in pairs}),
                unavailable_arms=len(rows)-len(ready)), 'readout_counts')
        require(type(out['rows']) is list and [x['measurement_key'] for x in out['rows']] == list(by_key),
                'saved_row_order')
        if out['schema'] == 'causes.derived_reader_readout.v1':
            require(peer['closed'] == out['closed'] and peer['closed_review'] == out['closed_review'], 'simple_custody_join')
            sources = {'single': dict(preparation=out['preparation'], input_review=out['input_review'],
                                      closed=out['closed'], closed_review=out['closed_review'])}
            choices = {x['measurement_key']: 'single' for x in ready}
        else:
            require(all(peer.get(k) == out[k] for k in ('mapping', 'mapping_review', 'sources')), 'composite_peer_join')
            mapping = rd.get(out['mapping'])
            rd.peer(out['mapping_review'], 'accepted_derived_reader_composite_mapping', mapping=out['mapping'])
            require(mapping.get('schema') == 'causes.derived_reader_composite_mapping.v1'
                    and mapping['sources'] == out['sources'] and mapping['preparation'] == out['preparation']
                    and mapping['specification'] == out['specification'], 'mapping_sources')
            mapped = mapping['rows']
            require([x['measurement_key'] for x in mapped] == [x['measurement_key'] for x in ready], 'mapping_order')
            choices = {x['measurement_key']: x['source'] for x in mapped}; sources = out['sources']
            require(set(choices.values()) == set(sources), 'mapping_source_roster')
        custody = {name: _custody(rd, value) for name, value in sources.items()}
        invalid = {}
        for item in peer.get('invalid_records', []):
            identity = (item['key'], item['instrument'])
            require(identity not in invalid and item['key'] in by_key and item['instrument'] in THRESHOLDS
                    and by_key[item['key']]['disposition'] == 'prepared'
                    and type(item.get('error')) is str and item['error'], 'invalid_annotation_record')
            invalid[identity] = item
        for index, (row, saved) in enumerate(zip(rows, out['rows'])):
            key = row['measurement_key']
            require(saved['disposition'] == row['disposition'] and set(saved['instruments']) == set(THRESHOLDS), 'row_instruments')
            _protocol(rd, prep, row, protocols)
            missing = row['disposition'] == 'unavailable'
            result_ref = request_root = None
            if missing:
                require(saved['state'] == 'unavailable' and saved['reason'] == row['reason']
                        and row['view'] is None and row['payloads'] == {} and 'result' not in saved,
                        'unavailable_row')
            else:
                native_prep, original, roots, qualified = custody[choices[key]]
                require(key in original and key in roots and all(original[key].get(k) == row.get(k) for k in REQUEST_FIELDS),
                        'physical_input_identity')
                _protocol(rd, native_prep, original[key], protocols)
                payload_protocol_seen = True
                if out['schema'] == 'causes.derived_reader_composite_readout.v1':
                    require(saved['source'] == choices[key], 'physical_source_choice')
                if key in qualified:
                    require(saved['state'] == 'unresolved_current_reader_attempt'
                            and saved.get('qualification') == qualified[key]['qualification']
                            and set(qualified[key]['missing']) == {name for name, item in saved['instruments'].items()
                                if item['status'] not in ('validated', 'validated_partial_observation')},
                            'qualified_terminal_state')
                else:
                    require(saved['state'] == 'finished', 'qualified_terminal_state')
                require(_hash(row['context_canonical_sha256']) and _hash(row['view_canonical_sha256']), 'prepared_context')
                result_ref = saved['result']; _pin(result_ref)
                request_root = roots[key] / key
                require(result_ref['path'] == str(request_root / 'RESULT.json')
                        and type(saved.get('evidence')) is dict
                        and _hash(saved['evidence'].get('relative_file_manifest_sha256')), 'physical_result')
            ids = {}
            for name in THRESHOLDS:
                native = saved['instruments'][name]
                require(type(native) is dict and set(native) == {'status', 'label'}, 'native_instrument_shape')
                label, status = native['label'], native['status']; failure = invalid.get((key, name))
                request = None
                if missing:
                    require(label is None and status == 'unavailable', 'unavailable_label')
                    state, reason, context = 'unavailable', row['reason'], None
                    identity = 'reader_unavailable_' + digest([key, name])
                else:
                    payload = row['payloads'][name]
                    request = dict(path=str(request_root / name / 'request.json'), bytes=payload['body_bytes']+1,
                                   sha256=payload['file_sha256'])
                    require(request['path'] not in physical_paths or physical_paths[request['path']] == request,
                            'physical_path_conflict')
                    physical_paths[request['path']] = request
                    identity = 'reader_physical_' + digest(request)
                    context = row['context_canonical_sha256']
                    require(status in ('validated', 'validated_partial_observation', 'technical_missing'), 'native_status')
                    if label is not None:
                        require(status in ('validated', 'validated_partial_observation') and label in READER_LABELS
                                and failure is None, 'observed_label')
                        state, reason = 'observed', None
                    else:
                        require(status != 'validated', 'validated_null_label')
                        state = 'technical_missing'
                        reason = failure['error'] if failure else ('technical_missing_reasoning' if
                            status == 'validated_partial_observation' else 'accepted_native_technical_missing')
                    if failure:
                        _pin(failure['result'])
                        require(label is None and failure['result']['path'] == str(request_root / name / 'result.json'),
                                'missing_result_identity')
                value = dict(id=identity, original_run_id=row['run_id'], stage='early', method='reader',
                    awareness_type='evaluation', threshold=name, context_sha256=context,
                    protocol_sha256=digest(protocols[name]), label=label, status=state, reason=reason)
                canonical = dict(request=request, result=result_ref, native_status=status,
                    context_key=key, view_kind=row['view_kind'], view_sha256=row.get('view_canonical_sha256'),
                    canonical_request_sha256=None if missing else row['payloads'][name]['canonical_sha256'])
                require(identity not in measurements or (measurements[identity] == value
                        and origins[identity]['physical'] == canonical), 'shared_measurement_conflict')
                require((key, name) not in keys or keys[(key, name)] == identity, 'shared_key_conflict')
                keys[(key, name)] = identity; measurements[identity] = value
                origins.setdefault(identity, dict(physical=canonical, aliases=[]))['aliases'].append(dict(
                    variant=descriptor['variant'], readout_row=index, source=None if missing else choices[key],
                    native_state=saved['state'], evidence=saved.get('evidence'), qualification=saved.get('qualification'),
                    invalid_record=failure))
                ids[name] = identity
            consumed.append(dict(variant=descriptor['variant'], measurement_key=key, instruments=ids))
    require(payload_protocol_seen, 'accepted_payload_protocol_evidence_required')
    result = dict(schema='causes.saved_neutral_reader_registry.v1', membership=membership,
        membership_review=membership_review, descriptors=copy.deepcopy(descriptors), protocols=copy.deepcopy(protocols),
        comparisons=copy.deepcopy(comparisons), measurements=list(measurements.values()), origins=origins,
        assignments=consumed, input_references=list(rd.references.values()), input_bytes=rd.total,
        input_bytes_definition='Unique metadata bytes; finish authenticates the same complete roster again.',
        inference_released=False, parser_replays=0, model_calls=0)
    require(len(encoded(result)) <= maximum_result_bytes, 'result_bound')
    rd.finish()
    return result
