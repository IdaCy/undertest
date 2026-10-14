"""Read a closed derived-reader cohort without new requests or missing-case filtering.

The caller supplies reviewed immutable selection, inputs and operational closure.
Every finished result is reconstructed by the existing native reader verifier.
This module returns a bounded JSON value; it never writes or runs a model.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import stat

from causes import campaign_analysis as analysis
from causes import campaign_reader as reader
from causes import campaign_reader_evidence as evidence
from causes import derived_reader_inputs as inputs


MAXIMUM_RESULT_BYTES = 2 * 1024 * 1024
IDENTITY = ('run_id', 'view_kind', 'stage')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _json(body):
    return json.loads(body, object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path
            and stat.S_ISREG(path.lstat().st_mode), 'canonical_regular_file_required')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def checked(reference):
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}, 'pin_required')
    require(pin(reference['path']) == reference, 'pinned_file_changed')
    body = Path(reference['path']).read_bytes()
    require(len(body) == reference['bytes'] and hashlib.sha256(body).hexdigest() == reference['sha256'],
            'pinned_file_changed')
    return _json(body)


def _accepted(reference, flag, **joins):
    value = checked(reference)
    require(value.get(flag) is True and value.get('findings') == [], 'accepted_peer_required:' + flag)
    require(all(value.get(key) == expected for key, expected in joins.items()), 'peer_join_changed:' + flag)
    return value


def _manifest(root):
    require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'capture_directory_required')
    rows = []
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        require(not path.is_symlink() and (stat.S_ISDIR(mode) or stat.S_ISREG(mode)), 'capture_special_file')
        if stat.S_ISREG(mode):
            reference = pin(path)
            rows.append([str(path.relative_to(root)), reference['bytes'], reference['sha256']])
    return dict(files=len(rows), bytes=sum(row[1] for row in rows),
                relative_file_manifest_sha256=hashlib.sha256(encoded(rows)).hexdigest())


def _identity(row):
    return tuple(row[key] for key in IDENTITY)


def _membership(spec, prep):
    assignments, ready, unavailable, pairs = [checked(prep[key]) for key in
                                             ('assignments', 'ready', 'unavailable', 'pairs')]
    require(all(type(rows) is list for rows in (assignments, ready, unavailable, pairs)), 'cohort_lists_required')
    for key in ('ready', 'unavailable', 'pairs'):
        require(spec[key] == prep[key], 'specification_cohort_changed')
    by_identity = {_identity(row): row for row in ready + unavailable}
    require(len(by_identity) == len(ready) + len(unavailable) == len(assignments), 'duplicate_or_missing_assignment')
    require(all(type(row) is dict and set(row) == set(IDENTITY) for row in assignments), 'assignment_shape_changed')
    require(len({_identity(row) for row in assignments}) == len(assignments), 'duplicate_assignment')
    require(set(by_identity) == {_identity(row) for row in assignments}, 'assignment_membership_changed')
    rows = [by_identity[_identity(row)] for row in assignments]
    require(ready == [row for row in rows if row['disposition'] == 'prepared']
            and unavailable == [row for row in rows if row['disposition'] == 'unavailable'], 'disposition_or_order_changed')
    keys = [row['measurement_key'] for row in rows]
    require(len(set(keys)) == len(keys), 'duplicate_measurement_key')
    by_key = dict(zip(keys, rows))
    members, pair_keys = {}, set()
    seen = set()
    for pair in pairs:
        task, draw = pair['task_id'], pair['draw_key']
        require((task, draw) not in pair_keys and set(pair['arms']) == {'baseline', 'treatment'}, 'duplicate_or_invalid_pair')
        pair_keys.add((task, draw))
        arms = members.setdefault(task, dict(control=[], treatment=[]))
        for arm, target in (('baseline', 'control'), ('treatment', 'treatment')):
            assigned = pair['arms'][arm]
            key = assigned['measurement_key']
            require(key in by_key and key not in seen, 'pair_key_membership_changed')
            row = by_key[key]
            require(assigned['run_id'] == row['run_id'] and assigned['disposition'] == row['disposition'],
                    'pair_identity_or_disposition_changed')
            seen.add(key)
            arms[target].append(key)
    require(seen == set(keys) and spec['assignment']['members'] == members, 'contrast_assignment_changed')
    require(spec['counts'] == dict(assigned_arms=len(rows), pairs=len(pairs), ready_arms=len(ready),
                                  tasks=len(members), unavailable_arms=len(unavailable)), 'frozen_counts_changed')
    require(all(len(a['control']) == len(a['treatment']) > 0 for a in members.values()), 'unbalanced_assigned_arms')
    return rows, ready, unavailable, pairs


def build_readout(*, specification, specification_review, preparation, input_review,
                  closed, closed_review, source_review, evidence_source):
    """Return independent descriptive contrasts after exact saved custody checks.

    Integrity mismatches fail closed. Native technical missingness and unresolved
    attempts remain explicit None labels; they never become negative outcomes.
    """
    own_source = pin(Path(__file__).resolve())
    sr = _accepted(source_review, 'accepted_derived_reader_readout_source', source=own_source)
    spec = checked(specification)
    prep = checked(preparation)
    _accepted(specification_review, 'accepted_frozen_derived_reader_readout_specification',
              specification=specification, preparation=preparation)
    _accepted(input_review, 'accepted_frozen_derived_reader_inputs', preparation=preparation, source=prep['source'])
    require(spec['schema'] == 'causes.frozen_derived_reader_readout_specification.v1'
            and spec['function'] == 'contrast' and spec['function_parameters'] == dict(release=False, family_size=10)
            and spec['inference_released'] is False and spec['planned_family_size'] == 10
            and spec['separate_direct_self_report_required'] is True
            and spec['measurement_type'] == 'independent_verbalized_awareness_reader'
            and spec['awareness_type'] == 'evaluation' and spec['thresholds'] == list(reader.INSTRUMENTS),
            'descriptive_independent_reader_contract_required')
    module_refs = dict(campaign_analysis=spec['analysis_source'], campaign_reader=prep['protocol']['reader_source'],
                       derived_reader_inputs=prep['source'], campaign_reader_evidence=evidence_source)
    modules = dict(campaign_analysis=analysis, campaign_reader=reader,
                   derived_reader_inputs=inputs, campaign_reader_evidence=evidence)
    require(sr.get('modules') == module_refs, 'readout_dependency_review_changed')
    for name, module in modules.items():
        require(pin(Path(module.__file__).resolve()) == module_refs[name], 'loaded_source_changed:' + name)
    actual = checked(closed)
    cp = _accepted(closed_review, 'accepted_derived_reader_batch_closed_cut', actual=closed,
                   prepared=preparation, root=actual['root'], plan=actual['plan'], journal=actual['journal'])
    require(cp.get('accepted_finite_reader_route_closed_cut') is True and cp.get('cohort_kind') == 'derived',
            'native_derived_closed_peer_required')
    require(actual['schema'] == 'causes.finite_reader_route_closed_cut.v1' and actual['prepared'] == preparation,
            'native_closed_preparation_changed')
    plan = checked(actual['plan'])
    require(plan['schema'] == 'causes.derived_reader_plan.v1' and plan['prepared'] == preparation
            and plan['previous_cohort_claimed_keys'] == [] and plan['additive_ready'] == prep['ready'],
            'whole_frozen_cohort_plan_required')
    rows, ready, unavailable, pairs = _membership(spec, prep)
    for value in (actual, cp):
        require(value['all_claims_accounted'] is True and value['assigned'] == len(rows)
                and value['ready'] == value['claimed'] == value['finished_or_unresolved'] == len(ready)
                and value['active'] == value['unclaimed'] == 0, 'closed_counts_changed')
    journal_ref = actual['journal']
    require(pin(journal_ref['path']) == journal_ref, 'closed_journal_changed')
    raw = Path(journal_ref['path']).read_bytes()
    require(raw.endswith(b'\n'), 'incomplete_closed_journal')
    journal = [_json(line) for line in raw.splitlines()]
    claims, terminal = [], {}
    for event in journal:
        if event['event'] == 'claimed':
            require(event['key'] not in claims, 'duplicate_claim')
            claims.append(event['key'])
        elif event['event'] in ('finished', 'unresolved_current_reader_attempt'):
            key = event['key']
            require(key in claims and key not in terminal, 'unclaimed_or_duplicate_terminal')
            terminal[key] = event
    require(claims == actual['claimed_keys'] and set(claims) == set(terminal)
            == {row['measurement_key'] for row in ready}, 'closed_claim_roster_changed')
    root = Path(actual['root'])
    require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'canonical_capture_root_required')
    config = checked(prep['protocol']['checker_config'])
    require(prep['prompts_directory'] == prep['protocol']['prompts_directory'], 'prompt_directory_changed')
    labels = {instrument: {} for instrument in reader.INSTRUMENTS}
    output, dispositions = [], Counter()
    # No response/judgment file is read before all source, selection and closure gates above.
    for row in rows:
        key = row['measurement_key']
        item = dict(measurement_key=key, disposition=row['disposition'], instruments={})
        verified = None
        if row['disposition'] == 'unavailable':
            rebuilt = inputs.unavailable_row(row['run_id'], row['view_kind'], row['stage'], row['reason'],
                                             row['provenance'], prep['protocol'])
            require(rebuilt == row, 'unavailable_assignment_changed')
            item.update(state='unavailable', reason=row['reason'])
        else:
            view = inputs.prepare_reader(row, prep, config, {'campaign_reader': reader})
            item['state'] = terminal[key]['event']
            if item['state'] == 'finished':
                destination = root / key
                before = _manifest(destination)
                verified = evidence.verify_reader_capture(destination, view['messages'], view['initial_context'],
                    view['phase'], config, prep['prompts_directory'], prep['protocol']['reader_source']['sha256'],
                    view['prefix_context'])
                require(before == _manifest(destination), 'capture_changed_during_verification')
                require(terminal[key]['instrument_statuses'] ==
                        {name: verified['instruments'][name]['status'] for name in reader.INSTRUMENTS},
                        'native_terminal_status_changed')
                item.update(evidence=before, result=pin(destination / 'RESULT.json'))
        dispositions[item['state']] += 1
        for instrument in reader.INSTRUMENTS:
            native = verified['instruments'][instrument] if verified is not None else None
            label = analysis.reader_label(native, spec['awareness_type'])
            labels[instrument][key] = label
            item['instruments'][instrument] = dict(status=native['status'] if native else item['state'], label=label)
        output.append(item)
    contrasts = {name: analysis.contrast(spec['assignment'], labels[name], release=False, family_size=10)
                 for name in reader.INSTRUMENTS}
    repositories = Counter()
    for task in spec['assignment']['members']:
        match = re.fullmatch(r'(.+__.+)-[0-9]+', task)
        require(match is not None, 'explicit_task_repository_identity_required')
        repositories[match.group(1)] += 1
    for name, module in modules.items():
        require(pin(Path(module.__file__).resolve()) == module_refs[name], 'source_changed_during_readout')
    for reference in (specification, specification_review, preparation, input_review, closed, closed_review, source_review):
        require(pin(reference['path']) == reference, 'custody_changed_during_readout')
    require(pin(journal_ref['path']) == journal_ref, 'journal_changed_during_readout')
    result = dict(schema='causes.derived_reader_readout.v1', source=own_source, source_review=source_review,
        modules=module_refs, specification=specification, specification_review=specification_review,
        preparation=preparation, input_review=input_review, closed=closed, closed_review=closed_review,
        journal=journal_ref, counts=spec['counts'], dispositions=dict(dispositions), rows=output, contrasts=contrasts,
        task_ids=sorted(spec['assignment']['members']), repository_task_counts=dict(sorted(repositories.items())),
        repository_key_rule='Frozen task identifier before its final numeric issue suffix.',
        interpretation=spec['analysis_contract'], inference_released=False, direct_self_report_included=False,
        model_calls=0, new_captures=0, outcome_filtering=False)
    require(len(encoded(result)) <= MAXIMUM_RESULT_BYTES, 'readout_output_bound')
    return result
