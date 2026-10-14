"""Resolve a fixed reader assignment across reviewed closed cohorts without calls.

A reviewed mapping chooses exactly one source for each available assigned arm.
Sources may be treatment-only cohorts or earlier cohorts with reusable controls.
Missing arms stay assigned, and labels come only from native capture verification.
"""
from collections import Counter
import importlib.util
from pathlib import Path
import re
import sys

from causes import derived_reader_readout as readout


MAXIMUM_RESULT_BYTES = 2 * 1024 * 1024
MAPPING_SCHEMA = 'causes.derived_reader_composite_mapping.v1'
SOURCE_FIELDS = {'preparation', 'input_review', 'closed', 'closed_review'}
REQUEST_FIELDS = (*readout.IDENTITY, 'measurement_key', 'disposition',
                  'view_canonical_sha256', 'protocol_sha256',
                  'context_canonical_sha256', 'targets_sha256', 'payloads')
require = readout.require


def _load_continuation(actual):
    reference = actual['source']
    readout._accepted(actual['source_review'], 'accepted_derived_reader_continuation_source', source=reference)
    require(readout.pin(reference['path']) == reference, 'continuation_source_changed')
    name = 'composite_continuation_' + reference['sha256']
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, reference['path'])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    module = sys.modules[name]
    require(readout.pin(module.__file__) == reference, 'loaded_continuation_source_changed')
    return module


def _union_source(descriptor, prep, rows, ready, actual):
    readout._accepted(descriptor['closed_review'], 'accepted_derived_reader_closed_union',
        union=descriptor['closed'], source=actual['source'], binding=actual['binding'],
        binding_review=actual['binding_review'], continuation_actual=actual['continuation_actual'],
        continuation_review=actual['continuation_review'])
    module = _load_continuation(actual)
    rebuilt = module.closed_union(actual['binding'], actual['binding_review'],
                                  actual['continuation_actual'], actual['continuation_review'])
    require(readout.encoded(rebuilt) == readout.encoded(actual), 'closed_union_changed')
    require(actual['inputs'] == descriptor['preparation'] and actual['inputs_review'] == descriptor['input_review']
            and actual['counts'] == prep['counts'], 'union_input_cohort_changed')
    locations = actual['context_locations']
    require(all(type(row) is dict and set(row) == {'key', 'root'} for row in locations)
            and [row['key'] for row in locations] == [row['measurement_key'] for row in ready],
            'union_location_roster_changed')
    roots = {row['key']: Path(row['root']) for row in locations}
    terminal, custody = {}, list(_pins(actual))
    all_claims = []
    for name in ('stopped_actual', 'continuation_actual'):
        cut = readout.checked(actual[name])
        custody.extend(cut[key] for key in ('plan', 'journal', 'journal_original', 'runtime_result'))
        raw = Path(cut['journal']['path']).read_bytes()
        require(readout.pin(cut['journal']['path']) == cut['journal'] and raw.endswith(b'\n'),
                'union_journal_changed')
        claims, events = [], {}
        for event in (readout._json(line) for line in raw.splitlines()):
            if event['event'] == 'claimed':
                require(event['key'] not in claims, 'union_duplicate_claim')
                claims.append(event['key'])
            elif event['event'] in ('finished', 'unresolved_current_reader_attempt'):
                key = event['key']
                require(key in claims and key not in events and key not in terminal,
                        'union_duplicate_or_unclaimed_terminal')
                require(roots.get(key) == Path(cut['root']), 'union_key_root_changed')
                events[key] = event
        require(claims == cut['claimed_keys'] and set(claims) == set(events), 'union_cut_membership_changed')
        all_claims.extend(claims)
        terminal.update(events)
    require(all_claims == list(roots), 'union_claim_order_changed')
    for root in set(roots.values()):
        require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'canonical_union_root_required')
    key = actual['original_unresolved_key']
    require([key] == [k for k, event in terminal.items() if event['event'] == 'unresolved_current_reader_attempt'],
            'union_unresolved_membership_changed')
    return dict(prep=prep, rows={row['measurement_key']: row for row in rows}, actual=actual,
        terminal=terminal, roots=roots, custody=custody, qualified={key: actual['original_missing_instruments']},
        config=readout.checked(prep['protocol']['checker_config']))


def _cohort(prep):
    require(prep['schema'] == 'causes.frozen_derived_reader_preparation.v1', 'frozen_input_schema_required')
    assignments, ready, unavailable = [readout.checked(prep[key]) for key in
                                        ('assignments', 'ready', 'unavailable')]
    require(all(type(rows) is list for rows in (assignments, ready, unavailable)), 'source_cohort_lists_required')
    require(all(type(row) is dict and set(row) == set(readout.IDENTITY) for row in assignments),
            'source_assignment_shape_changed')
    by_identity = {readout._identity(row): row for row in ready + unavailable}
    require(len(by_identity) == len(ready) + len(unavailable) == len(assignments)
            and len({readout._identity(row) for row in assignments}) == len(assignments)
            and set(by_identity) == {readout._identity(row) for row in assignments},
            'source_assignment_membership_changed')
    rows = [by_identity[readout._identity(row)] for row in assignments]
    require(ready == [row for row in rows if row['disposition'] == 'prepared']
            and unavailable == [row for row in rows if row['disposition'] == 'unavailable'],
            'source_disposition_or_order_changed')
    require(len({row['measurement_key'] for row in rows}) == len(rows), 'source_duplicate_measurement_key')
    require(prep['counts'] == dict(assigned=len(rows), ready=len(ready), unavailable=len(unavailable),
                                   requests=len(ready) * len(readout.reader.INSTRUMENTS)), 'source_counts_changed')
    return rows, ready, unavailable


def _source(descriptor, input_source, protocol_identity):
    require(type(descriptor) is dict and set(descriptor) == SOURCE_FIELDS, 'source_descriptor_shape_changed')
    prep = readout.checked(descriptor['preparation'])
    readout._accepted(descriptor['input_review'], 'accepted_frozen_derived_reader_inputs',
                      preparation=descriptor['preparation'], source=prep['source'])
    require(prep['source'] == input_source, 'source_input_adapter_changed')
    require(readout.inputs.protocol_identity(prep['protocol']) == protocol_identity
            and prep['prompts_directory'] == prep['protocol']['prompts_directory'], 'source_protocol_changed')
    rows, ready, unavailable = _cohort(prep)
    actual = readout.checked(descriptor['closed'])
    if actual.get('schema') == 'causes.derived_reader_closed_union.v1':
        return _union_source(descriptor, prep, rows, ready, actual)
    cp = readout._accepted(descriptor['closed_review'], 'accepted_derived_reader_batch_closed_cut',
        actual=descriptor['closed'], prepared=descriptor['preparation'], root=actual['root'],
        plan=actual['plan'], journal=actual['journal'])
    require(cp.get('accepted_finite_reader_route_closed_cut') is True and cp.get('cohort_kind') == 'derived',
            'native_derived_closed_peer_required')
    require(actual['schema'] == 'causes.finite_reader_route_closed_cut.v1'
            and actual['prepared'] == descriptor['preparation'], 'source_closed_preparation_changed')
    plan = readout.checked(actual['plan'])
    require(plan['schema'] == 'causes.derived_reader_plan.v1' and plan['prepared'] == descriptor['preparation']
            and plan['previous_cohort_claimed_keys'] == [] and plan['additive_ready'] == prep['ready'],
            'whole_source_cohort_plan_required')
    for value in (actual, cp):
        require(value['all_claims_accounted'] is True and value['assigned'] == len(rows)
                and value['ready'] == value['claimed'] == value['finished_or_unresolved'] == len(ready)
                and value['active'] == value['unclaimed'] == 0, 'source_closed_counts_changed')
    journal_ref = actual['journal']
    require(readout.pin(journal_ref['path']) == journal_ref, 'source_journal_changed')
    raw = Path(journal_ref['path']).read_bytes()
    require((raw.endswith(b'\n') or not raw) and readout.pin(journal_ref['path']) == journal_ref,
            'source_journal_incomplete_or_changed')
    claims, terminal = [], {}
    for event in (readout._json(line) for line in raw.splitlines()):
        if event['event'] == 'claimed':
            require(event['key'] not in claims, 'source_duplicate_claim')
            claims.append(event['key'])
        elif event['event'] in ('finished', 'unresolved_current_reader_attempt'):
            key = event['key']
            require(key in claims and key not in terminal, 'source_unclaimed_or_duplicate_terminal')
            terminal[key] = event
    require(claims == actual['claimed_keys'] and set(claims) == set(terminal)
            == {row['measurement_key'] for row in ready}, 'source_closed_claim_roster_changed')
    root = Path(actual['root'])
    require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'canonical_source_root_required')
    return dict(prep=prep, rows={row['measurement_key']: row for row in rows}, actual=actual,
                terminal=terminal, roots={key: root for key in terminal}, qualified={},
                custody=[actual['journal'], actual['plan']],
                config=readout.checked(prep['protocol']['checker_config']))


def _pins(value):
    if type(value) is dict:
        if set(value) == {'path', 'bytes', 'sha256'}:
            yield value
        else:
            for child in value.values():
                yield from _pins(child)
    elif type(value) is list:
        for child in value:
            yield from _pins(child)


def build_composite_readout(*, specification, specification_review, preparation, input_review,
                            mapping, mapping_review, source_review, evidence_source):
    """Return descriptive native contrasts for one exhaustive reviewed assignment.

    Mapping rows are ordered like the global ready list and contain only
    measurement_key and source. Source descriptors name preparation, input_review,
    closed and closed_review pins; each must be used. A prior cohort may contain
    extra arms, but only explicitly selected exact requests are resolved.
    """
    own_source = readout.pin(Path(__file__).resolve())
    sr = readout._accepted(source_review, 'accepted_derived_reader_composite_source', source=own_source)
    spec, prep, selected = [readout.checked(ref) for ref in (specification, preparation, mapping)]
    readout._accepted(specification_review, 'accepted_frozen_derived_reader_readout_specification',
                      specification=specification, preparation=preparation)
    readout._accepted(input_review, 'accepted_frozen_derived_reader_inputs', preparation=preparation, source=prep['source'])
    readout._accepted(mapping_review, 'accepted_derived_reader_composite_mapping',
                      mapping=mapping, specification=specification, preparation=preparation)
    require(spec['schema'] == 'causes.frozen_derived_reader_readout_specification.v1'
            and spec['function'] == 'contrast' and spec['function_parameters'] == dict(release=False, family_size=10)
            and spec['inference_released'] is False and spec['planned_family_size'] == 10
            and spec['separate_direct_self_report_required'] is True
            and spec['measurement_type'] == 'independent_verbalized_awareness_reader'
            and spec['awareness_type'] == 'evaluation' and spec['thresholds'] == list(readout.reader.INSTRUMENTS),
            'descriptive_independent_reader_contract_required')
    modules = dict(derived_reader_readout=readout, campaign_analysis=readout.analysis, campaign_reader=readout.reader,
                   derived_reader_inputs=readout.inputs, campaign_reader_evidence=readout.evidence)
    module_refs = dict(derived_reader_readout=readout.pin(Path(readout.__file__).resolve()),
        campaign_analysis=spec['analysis_source'], campaign_reader=prep['protocol']['reader_source'],
        derived_reader_inputs=prep['source'], campaign_reader_evidence=evidence_source)
    require(sr.get('modules') == module_refs, 'composite_dependency_review_changed')
    for name, module in modules.items():
        require(readout.pin(Path(module.__file__).resolve()) == module_refs[name], 'loaded_source_changed:' + name)
    rows, ready, unavailable, pairs = readout._membership(spec, prep)
    require(_cohort(prep) == (rows, ready, unavailable), 'global_cohort_changed')
    require(prep['prompts_directory'] == prep['protocol']['prompts_directory'], 'global_prompt_directory_changed')
    protocol_identity = readout.inputs.protocol_identity(prep['protocol'])
    config = readout.checked(prep['protocol']['checker_config'])
    require(set(selected) == {'schema', 'specification', 'preparation', 'sources', 'rows'}
            and selected['schema'] == MAPPING_SCHEMA and selected['specification'] == specification
            and selected['preparation'] == preparation, 'composite_mapping_scope_changed')
    descriptors = selected['sources']
    require(type(descriptors) is dict and (descriptors or not ready)
            and all(type(name) is str and name for name in descriptors), 'composite_sources_required')
    mapping_rows = selected['rows']
    require(type(mapping_rows) is list and all(type(row) is dict and set(row) == {'measurement_key', 'source'}
            for row in mapping_rows), 'composite_mapping_shape_changed')
    require([row['measurement_key'] for row in mapping_rows] == [row['measurement_key'] for row in ready],
            'composite_mapping_not_exact_ready_order')
    require({row['source'] for row in mapping_rows} == set(descriptors), 'composite_mapping_source_set_changed')
    sources = {name: _source(descriptor, prep['source'], protocol_identity) for name, descriptor in descriptors.items()}
    source_roots = [root for source in sources.values() for root in set(source['roots'].values())]
    require(len(set(source_roots)) == len(source_roots), 'duplicate_source_capture_root')
    choice = {row['measurement_key']: row['source'] for row in mapping_rows}
    contexts = {}
    # Validate every selected request before any response/judgment file is read.
    for row in ready:
        key = row['measurement_key']
        source = sources[choice[key]]
        require(key in source['rows'] and key in source['terminal'], 'selected_source_key_missing')
        original = source['rows'][key]
        require(all(original.get(field) == row.get(field) for field in REQUEST_FIELDS), 'selected_request_identity_changed')
        view = readout.inputs.prepare_reader(row, prep, config, {'campaign_reader': readout.reader})
        original_view = readout.inputs.prepare_reader(original, source['prep'], source['config'],
                                                      {'campaign_reader': readout.reader})
        require(readout.reader.encoded(view) == readout.reader.encoded(original_view), 'selected_full_view_changed')
        contexts[key] = original_view
    for row in unavailable:
        require(readout.inputs.unavailable_row(row['run_id'], row['view_kind'], row['stage'], row['reason'],
            row['provenance'], prep['protocol']) == row, 'unavailable_assignment_changed')
    custody = [own_source, specification, specification_review, preparation, input_review, mapping, mapping_review, source_review,
               *module_refs.values(), *list(_pins(prep)), *list(_pins(selected)),
               *[ref for source in sources.values() for ref in source['custody']]]
    for source in sources.values():
        custody.extend(_pins(source['prep']))
    for row in rows:
        custody.extend(_pins(row))
    for key, source_name in choice.items():
        custody.extend(_pins(sources[source_name]['rows'][key]))
    immutable = {}
    for reference in custody:
        require(reference['path'] not in immutable or immutable[reference['path']] == reference,
                'conflicting_immutable_pins')
        immutable[reference['path']] = reference
    for reference in immutable.values():
        require(readout.pin(reference['path']) == reference, 'composite_custody_changed')
    labels = {name: {} for name in readout.reader.INSTRUMENTS}
    output, dispositions, usage, manifests = [], Counter(), Counter(), {}
    for row in rows:
        key = row['measurement_key']
        item = dict(measurement_key=key, disposition=row['disposition'], instruments={})
        verified = None
        if row['disposition'] == 'unavailable':
            item.update(state='unavailable', reason=row['reason'], source=None)
        else:
            name = choice[key]
            source, view = sources[name], contexts[key]
            terminal = source['terminal'][key]
            item.update(state=terminal['event'], source=name)
            usage[name] += 1
            if item['state'] == 'finished' or key in source['qualified']:
                destination = source['roots'][key] / key
                before = readout._manifest(destination)
                verified = readout.evidence.verify_reader_capture(destination, view['messages'], view['initial_context'],
                    view['phase'], source['config'], source['prep']['prompts_directory'],
                    source['prep']['protocol']['reader_source']['sha256'], view['prefix_context'])
                require(before == readout._manifest(destination), 'capture_changed_during_verification')
                if item['state'] == 'finished':
                    require(terminal['instrument_statuses'] == {name: verified['instruments'][name]['status']
                            for name in readout.reader.INSTRUMENTS}, 'native_terminal_status_changed')
                else:
                    missing = source['qualified'][key]
                    require(set(missing) == {name for name, value in verified['instruments'].items()
                            if value['status'] not in ('validated', 'validated_partial_observation')},
                            'qualified_instrument_missingness_changed')
                    require(all(verified['instruments'][name].get('validation_error', '').startswith(
                        'evidence_not_exact_channel_substring:') for name in missing),
                        'qualified_citation_failure_changed')
                    item['qualification'] = source['actual']['qualification']
                item.update(evidence=before, result=readout.pin(destination / 'RESULT.json'))
                manifests[destination] = before
        dispositions[item['state']] += 1
        for name in readout.reader.INSTRUMENTS:
            native = verified['instruments'][name] if verified is not None else None
            label = readout.analysis.reader_label(native, spec['awareness_type'])
            labels[name][key] = label
            item['instruments'][name] = dict(status=native['status'] if native else item['state'], label=label)
        output.append(item)
    contrasts = {name: readout.analysis.contrast(spec['assignment'], values, release=False, family_size=10)
                 for name, values in labels.items()}
    repositories = Counter()
    for task in spec['assignment']['members']:
        match = re.fullmatch(r'(.+__.+)-[0-9]+', task)
        require(match is not None, 'explicit_task_repository_identity_required')
        repositories[match.group(1)] += 1
    for reference in immutable.values():
        require(readout.pin(reference['path']) == reference, 'composite_custody_changed_during_readout')
    for destination, manifest in manifests.items():
        require(readout._manifest(destination) == manifest, 'capture_changed_during_readout')
    for name, source in sources.items():
        if source['qualified']:
            require(_source(descriptors[name], prep['source'], protocol_identity) == source,
                    'union_changed_during_readout')
    result = dict(schema='causes.derived_reader_composite_readout.v1', source=own_source, source_review=source_review,
        modules=module_refs, specification=specification, specification_review=specification_review,
        preparation=preparation, input_review=input_review, mapping=mapping, mapping_review=mapping_review,
        sources=descriptors, source_usage=dict(usage), counts=spec['counts'], dispositions=dict(dispositions),
        rows=output, contrasts=contrasts, task_ids=sorted(spec['assignment']['members']),
        repository_task_counts=dict(sorted(repositories.items())), interpretation=spec['analysis_contract'],
        inference_released=False, direct_self_report_included=False, model_calls=0, new_captures=0, outcome_filtering=False)
    require(len(readout.encoded(result)) <= MAXIMUM_RESULT_BYTES, 'composite_readout_output_bound')
    return result
