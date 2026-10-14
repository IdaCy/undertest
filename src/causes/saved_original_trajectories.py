"""Select native rows from an accepted fixed-study assembly, without replay.

This is distinct from the later measurement-segment schema. Assignment records
describe planned instruments; they do not fabricate physical requests or infer
labels. Callers separately authorize the exact source, inputs and output write.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from shared.evidence_bytes import read_pinned


INPUTS = frozenset(('assembly_result', 'assembly_review', 'fixed_metadata',
                    'descriptive_inputs', 'reader_freeze', 'mapping',
                    'mapping_review', 'mapping_adoption'))
LIMITS = frozenset(('metadata_file_bytes', 'metadata_total_bytes',
                    'payload_bytes', 'output_bytes'))
THRESHOLDS = ('presence', 'strongly_favors', 'leans_towards_or_favors')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _float(value):
    number = float(value)
    require(math.isfinite(number), 'nonfinite_json')
    return number


def _json(raw):
    def constant(value):
        raise ValueError('nonfinite_json:' + value)
    return json.loads(raw, object_pairs_hook=_object, parse_float=_float,
                      parse_constant=constant)


class _Metadata:
    def __init__(self, limits):
        self.limits, self.bytes, self.refs = limits, 0, {}

    def get(self, ref, *, decode=True):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
                and type(ref['bytes']) is int and ref['bytes'] > 0,
                'exact_metadata_pin')
        require(ref['path'] not in self.refs or self.refs[ref['path']] == ref,
                'conflicting_metadata_pin')
        require(self.bytes + ref['bytes'] <= self.limits['metadata_total_bytes'],
                'metadata_total_bound')
        body = read_pinned(ref, limit=self.limits['metadata_file_bytes'])
        self.bytes += len(body)
        self.refs[ref['path']] = copy.deepcopy(ref)
        return _json(body) if decode else body

    def finish(self):
        for ref in list(self.refs.values()):
            self.get(ref, decode=False)


def _targets(mapping, fixed):
    targets = {}
    require(type(mapping['comparisons']) is list and mapping['comparisons'], 'comparison_roster')
    for comparison in mapping['comparisons']:
        require(comparison.get('stage') == 'early', 'early_comparison_required')
        for member in comparison['members']:
            require(member['disposition'] in ('included', 'unsupported'), 'member_disposition')
            if member['disposition'] == 'unsupported':
                continue
            require(type(member['draw_key']) is int and member['draw_key'] >= 0
                    and set(member['arms']) == {'baseline', 'treatment'}, 'member_identity')
            for arm, value in member['arms'].items():
                run = value['original_run_id']
                require(run in fixed and fixed[run]['terminal'] is True, 'target_outside_fixed_domain')
                variant = comparison['baseline' if arm == 'baseline' else 'variant']
                identity = (member['task_id'], variant, member['draw_key'])
                require(identity == (fixed[run]['task_id'], fixed[run]['variant'], fixed[run]['repetition']),
                        'target_identity_mismatch')
                require(run not in targets or targets[run] == identity, 'conflicting_target_identity')
                targets[run] = identity
    require(targets, 'empty_target_roster')
    return sorted(targets)


def select_fixed_trajectories(*, inputs, limits):
    """Return complete selected native objects and explicit instrument assignment.

    Eight exact input pins are required (``INPUTS``). Assembly and mapping peers
    are authenticated before payload I/O. The accepted binding supplies the
    source and expected native count. The fixed run metadata supplies the full
    run domain and task/variant/repetition/seed identities; descriptive and
    reader rosters are independently joined to native allocation fields.

    Metadata totals count every read, including one final recheck. The complete
    JSONL payload is hash-verified and read once. Output bounds cover compact
    ``encoded(result)`` plus one newline. This function writes nothing and does
    not call an actor, reader, parser, verifier, or statistical routine.
    """
    require(type(inputs) is dict and set(inputs) == INPUTS, 'exact_input_roster')
    require(type(limits) is dict and set(limits) == LIMITS
            and all(type(v) is int and 0 < v <= 512 * 1024**2 for v in limits.values()),
            'positive_exact_limits')
    read = _Metadata(dict(limits))
    result, peer = read.get(inputs['assembly_result']), read.get(inputs['assembly_review'])
    require(peer.get('accepted_mi_final_input_assembly') is True and peer.get('findings') == []
            and peer['result'] == inputs['assembly_result']
            and peer['source'] == result['source'] and peer['binding'] == result['binding'],
            'accepted_assembly_required')
    read.get(result['source'], decode=False)
    binding = read.get(result['binding'])
    payload = peer['trajectories']
    require(sum(ref == payload for ref in result['outputs']) == 1
            and type(payload['bytes']) is int and 0 < payload['bytes'] <= limits['payload_bytes'],
            'exact_bounded_trajectory_payload')
    require(binding['descriptive_inputs'] == inputs['descriptive_inputs']
            and binding['freeze_binding'] == inputs['reader_freeze']
            and binding['expected_record_count'] == result['records'] == peer['rows']
            and type(result['records']) is int and result['records'] > 0
            and type(binding['model']) is str and binding['model'], 'assembly_input_binding')

    metadata = read.get(inputs['fixed_metadata'])
    fixed = {}
    require(type(metadata) is list and metadata, 'fixed_metadata_roster')
    for row in metadata:
        require(type(row) is dict and type(row['run_id']) is str and row['run_id']
                and row['run_id'] not in fixed and type(row['terminal']) is bool
                and type(row['repetition']) is int and row['repetition'] >= 0
                and type(row['seed']) is int and type(row['task_id']) is str
                and type(row['variant']) is str, 'fixed_metadata_identity')
        fixed[row['run_id']] = row
    terminal = {run for run, row in fixed.items() if row['terminal']}
    require(len(terminal) == result['records'], 'fixed_terminal_count')
    descriptive = read.get(inputs['descriptive_inputs'])
    threshold_rows = descriptive['rosters']['threshold_early']
    require(type(threshold_rows) is list and len(set(threshold_rows)) == len(threshold_rows)
            and set(threshold_rows) <= set(fixed), 'threshold_assignment_roster')
    threshold = set(threshold_rows)
    freeze = read.get(inputs['reader_freeze'])
    reader_keys = [(row['run_id'], row['stage']) for row in freeze['slots']]
    require(freeze['model'] == binding['model'] and descriptive['model'] == binding['model']
            and len(set(reader_keys)) == len(reader_keys)
            and all(run in fixed and stage in ('early', 'end') for run, stage in reader_keys)
            and {run for run, stage in reader_keys if stage == 'early'} == threshold,
            'reader_threshold_roster_join')

    mapping = read.get(inputs['mapping'])
    mapping_peer = read.get(inputs['mapping_review'])
    adoption = read.get(inputs['mapping_adoption'])
    require(mapping_peer.get('accepted_frozen_neutral_matched_membership') is True
            and mapping_peer.get('findings') == [] and mapping_peer['mapping'] == inputs['mapping']
            and mapping_peer['actual'] == inputs['mapping']
            and adoption.get('accepted') is True and adoption.get('findings') == []
            and adoption['mapping'] == inputs['mapping']
            and adoption['independent_review'] == inputs['mapping_review'], 'accepted_mapping_required')
    targets = _targets(mapping, fixed)
    wanted, seen, selected = set(targets), set(), {}
    require(payload['path'] not in read.refs, 'payload_metadata_alias')
    body = read_pinned(payload, limit=limits['payload_bytes'])
    require(body.endswith(b'\n'), 'jsonl_final_newline_required')
    offset = 0
    for ordinal, line in enumerate(body.splitlines(keepends=True)):
        require(line.endswith(b'\n') and line.strip(), 'nonempty_jsonl_row')
        row = _json(line)
        require(type(row) is dict and type(row.get('run_id')) is str
                and row['run_id'] in terminal and row['run_id'] not in seen, 'trajectory_run_domain')
        run, allocation = row['run_id'], row['allocation']
        expected = fixed[run]
        require(row['model'] == binding['model'] and row['task_id'] == expected['task_id']
                and row['variant_id'] == expected['variant']
                and allocation['run_id'] == run and allocation['canonical_variant'] == row['variant_id']
                and type(allocation['repetition']) is int and allocation['repetition'] == expected['repetition']
                and type(allocation['seed']) is int and allocation['seed'] == expected['seed'],
                'trajectory_allocation_identity')
        instruments, stages = allocation['instruments'], allocation['reader_stages']
        require(type(instruments) is list and all(type(x) is str for x in instruments)
                and len(set(instruments)) == len(instruments)
                and type(stages) is list and len(set(stages)) == len(stages)
                and set(stages) <= {'early', 'end'}, 'allocation_instrument_shape')
        require('evaluation_presence' in instruments
                and all(('evaluation_' + name in instruments) == (run in threshold) for name in THRESHOLDS[1:])
                and ('early' in stages) == (run in threshold)
                and (run not in threshold or allocation['reader_thresholds'] == list(THRESHOLDS)),
                'allocation_assignment_roster')
        seen.add(run)
        if run in wanted:
            assignments = []
            for method in ('self', 'reader'):
                for name in THRESHOLDS:
                    assigned = ('evaluation_' + name in instruments) if method == 'self' else 'early' in stages
                    assignments.append(dict(method=method, threshold=name,
                        disposition='assigned' if assigned else 'not_assigned'))
            selected[run] = dict(run_id=run, native=row, assignments=assignments,
                provenance=dict(payload=copy.deepcopy(payload), row_index=ordinal,
                    byte_offset=offset, line_bytes=len(line),
                    line_sha256=hashlib.sha256(line).hexdigest(),
                    native_canonical_sha256=hashlib.sha256(encoded(row)).hexdigest()))
        offset += len(line)
    require(offset == len(body) and seen == terminal and len(seen) == result['records'],
            'complete_trajectory_domain')
    require(set(selected) == wanted, 'incomplete_selected_rows')
    read.finish()
    output = dict(schema='causes.saved_original_fixed_trajectory_subset.v1', inputs=copy.deepcopy(inputs),
        payload=copy.deepcopy(payload), target_runs=targets, selected=[selected[run] for run in targets],
        complete_records_checked=len(seen), fixed_domain=len(fixed),
        assignment_counts={status:sum(a['disposition'] == status for r in selected.values() for a in r['assignments'])
                           for status in ('assigned', 'not_assigned')},
        metadata_bytes_read=read.bytes, payload_bytes_read=len(body), payload_reads=1,
        limits=dict(limits), native_rows_unchanged=True, physical_measurement_ids_assigned=False,
        context_protocol_joins_resolved=False, outcomes_reclassified=False, inference_released=False)
    require(len(encoded(output)) + 1 <= limits['output_bytes'], 'output_bound')
    return output
