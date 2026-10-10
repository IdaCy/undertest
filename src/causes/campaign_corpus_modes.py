"""Pure assignment-mode partitions of complete, explicitly supplied custody prefixes."""

import hashlib
import json

from causes import campaign_corpus_count as counter


def digest_ids(ids):
    return hashlib.sha256(json.dumps(sorted(ids), separators=(',', ':')).encode()).hexdigest()


def partition_prefixes(prefixes, registry, root_models, frozen_modes,
                       administrative_ids, excluded_roots):
    """Partition custody membership using frozen original-ID Boolean assignments.

    ``frozen_modes`` maps each registered model to an original run-ID -> bool dictionary: true means selected full, false means selected prefix. ``administrative_ids`` supplies a distinct ID list for every model. Callers retain responsibility for the provenance of both frozen inputs.

    Extra-corpus modes remain unknown, even if a custody row supplies mode or outcome fields. Generated means present in these prefixes, not successful or naturally completed. Unresolved does not distinguish active from unissued. No referenced payload, host or filesystem path is opened.
    """
    counter.require(type(frozen_modes) is dict and frozen_modes,
                    'explicit_frozen_mode_mapping_required')
    for roster in frozen_modes.values():
        counter.require(type(roster) is dict, 'frozen_mode_roster_object_required')
        counter.require(all(type(value) is bool for value in roster.values()),
                        'strict_boolean_assignment_mode_required')
    frozen_ids = {model: list(roster) for model, roster in frozen_modes.items()}
    counted = counter.count_prefixes(prefixes, registry, root_models,
                                     frozen_ids, excluded_roots)
    counter.require(type(administrative_ids) is dict
                    and set(administrative_ids) == set(frozen_modes),
                    'exact_administrative_models_required')
    admin = {}
    for model, values in administrative_ids.items():
        counter.require(type(values) is list, 'explicit_administrative_ID_list_required')
        counter.require(all(type(rid) is str for rid in values),
                        'administrative_ID_string_required')
        counter.require(len(set(values)) == len(values), 'duplicate_administrative_ID')
        admin[model] = set(values)
        counter.require(admin[model] <= set(frozen_modes[model]),
                        'administrative_ID_not_in_model_roster')

    present = {model: set() for model in frozen_modes}
    by_root = {root: {'model_key': model, 'finalized_records': 0,
                      'selected_full': 0, 'selected_prefix': 0,
                      'mode_unknown_extra_corpus': 0}
               for root, model in root_models.items()}
    root_ids = {root: [] for root in by_root}
    for prefix in prefixes:
        for row in counter.complete_rows(prefix['raw']):
            path = counter.registry_api.relative(row['run'])
            root, rid = str(path.parent), path.name
            if root in excluded_roots:
                continue
            model = root_models[root]
            root_ids[root].append(rid)
            values = by_root[root]
            values['finalized_records'] += 1
            if rid in frozen_modes[model]:
                present[model].add(rid)
                label = 'selected_full' if frozen_modes[model][rid] else 'selected_prefix'
                values[label] += 1
            else:
                values['mode_unknown_extra_corpus'] += 1

    models = {}
    for model, roster in frozen_modes.items():
        counter.require(not present[model] & admin[model],
                        'generated_administrative_ID_overlap')
        unresolved = set(roster) - present[model] - admin[model]
        modes = {}
        for label, full in [('selected_full', True), ('selected_prefix', False)]:
            ids = {rid for rid, value in roster.items() if value is full}
            groups = {'assigned': ids,
                      'generated_custody_final': ids & present[model],
                      'administratively_unavailable': ids & admin[model],
                      'unresolved': ids & unresolved}
            modes[label] = {key: {'count': len(group), 'id_set_sha256': digest_ids(group)}
                            for key, group in groups.items()}
            counter.require(len(ids) == sum(len(groups[key]) for key in
                            ('generated_custody_final', 'administratively_unavailable',
                             'unresolved')), 'assignment_state_partition_failed')
        totals = {key: sum(value[key] for value in by_root.values()
                           if value['model_key'] == model)
                  for key in ('finalized_records', 'selected_full', 'selected_prefix',
                              'mode_unknown_extra_corpus')}
        expected = counted['counts'][model]
        counter.require(totals['finalized_records'] == expected['total']
                        and totals['selected_full'] + totals['selected_prefix']
                        == expected['fixed_generated']
                        and totals['mode_unknown_extra_corpus'] == expected['extra_corpus'],
                        'assignment_custody_partition_failed')
        models[model] = {'model_key': model, 'finalized_assignment_modes': totals,
                         'frozen_fixed_assignment_states': modes}
    for root, values in by_root.items():
        counter.require(values['finalized_records'] == counted['by_root'][root]['total'],
                        'root_assignment_partition_failed')
        values['run_id_set_sha256'] = digest_ids(root_ids[root])

    return {'schema': 'causes.corpus_assignment_modes.v1',
            'models': models, 'by_root': by_root, 'inputs': counted['inputs'],
            'total_finalized_records': counted['total_production_finalized'],
            'excluded_by_root': counted['excluded_by_root'],
            'excluded_rows': counted['excluded_rows'],
            'lower_bound': True, 'success_filtered': False,
            'semantic_status_inspected': False, 'payloads_read': False}
