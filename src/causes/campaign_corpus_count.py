import hashlib
import json

from causes import campaign_handoff as h
from causes import campaign_registry as registry_api


def require(value, reason):
    if not value:
        raise ValueError(reason)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_custody_object_key')
        result[key] = value
    return result


def complete_rows(raw):
    require(type(raw) is bytes and (not raw or raw.endswith(b'\n')),
            'complete_custody_prefix_required')
    rows = []
    for line in raw.splitlines():
        require(bool(line.strip()), 'blank_custody_row')
        row = json.loads(line, object_pairs_hook=unique_object)
        require(type(row) is dict, 'custody_row_object_required')
        registry_api.relative(row['run'])
        rows.append(row)
    return rows


def count_prefixes(prefixes, registry, root_models, frozen_ids, excluded_roots):
    """Count sealed custody metadata without opening any referenced run payload."""
    registry = registry_api.validate_registry(registry)
    require(type(root_models) is dict and set(root_models) == set(registry['sync_roots']),
            'exact_production_root_mapping_required')
    require(set(frozen_ids) == set(registry['fixed_roots'])
            and set(root_models.values()) == set(frozen_ids), 'exact_corpus_models_required')
    excluded = registry_api.distinct_roots(excluded_roots)
    require(not excluded & set(root_models), 'excluded_production_root_overlap')
    for model, roots in registry['fixed_roots'].items():
        require(all(root_models[root] == model for root in roots), 'fixed_root_model_changed')
    # Reuse the registered original-ID validation before processing any indices.
    registry_api.fixed_rows([], frozen_ids, registry)
    frozen_owner = {rid: model for model, ids in frozen_ids.items() for rid in ids}
    require(type(prefixes) is list and prefixes, 'explicit_custody_prefixes_required')
    labels, locations, ids, all_rows, inputs = set(), {}, {}, [], []
    by_root = {root: {'model': model, 'total': 0, 'fixed': 0, 'extra': 0}
               for root, model in root_models.items()}
    excluded_counts = dict.fromkeys(excluded_roots, 0)
    for prefix in prefixes:
        label, raw = prefix['label'], prefix['raw']
        require(type(label) is str and label and label not in labels,
                'distinct_custody_prefix_labels_required')
        labels.add(label)
        rows = complete_rows(raw)
        for row in rows:
            path = registry_api.relative(row['run'])
            root, rid = str(path.parent), path.name
            require(root in root_models or root in excluded, 'unknown_custody_root')
            require(row['run'] not in locations, 'duplicate_custody_run')
            require(rid not in ids, 'custody_ID_collision')
            locations[row['run']], ids[rid] = label, row['run']
            if root in excluded:
                require(rid not in frozen_owner, 'original_ID_in_excluded_root')
                excluded_counts[root] += 1
                continue
            model = root_models[root]
            fixed_model = frozen_owner.get(rid)
            if fixed_model is not None:
                require(fixed_model == model, 'original_ID_model_changed')
                require(root in registry['fixed_roots'][model],
                        'original_ID_in_unregistered_root')
            by_root[root]['total'] += 1
            by_root[root]['fixed' if fixed_model is not None else 'extra'] += 1
            all_rows.append(row)
        inputs.append({'label': label, 'bytes': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest(), 'rows': len(rows)})
    joined = registry_api.fixed_rows(all_rows, frozen_ids, registry)
    counts = {model: {'total': 0, 'fixed_generated': len(joined[model]), 'extra_corpus': 0}
              for model in frozen_ids}
    for row in by_root.values():
        counts[row['model']]['total'] += row['total']
        counts[row['model']]['extra_corpus'] += row['extra']
    for row in counts.values():
        require(row['total'] == row['fixed_generated'] + row['extra_corpus'],
                'custody_count_partition_failed')
    return {'schema': 'causes.model_corpus_custody_count.v1', 'inputs': inputs,
            'counts': counts, 'by_root': by_root, 'excluded_by_root': excluded_counts,
            'total_production_finalized': sum(row['total'] for row in counts.values()),
            'excluded_rows': sum(excluded_counts.values()),
            'lower_bound': True, 'success_filtered': False,
            'semantic_status_inspected': False, 'payloads_read': False}


def count_bound_prefixes(binding):
    """Consume exact already-sealed files; acquire no new index or host snapshot."""
    require(binding['schema'] == 'causes.model_corpus_count_binding.v1',
            'corpus_count_binding_schema')
    registry = h.checked(binding['registry'])
    mapping = h.checked(binding['mapping'])
    frozen_ids = {model: h.checked(reference)
                  for model, reference in binding['frozen_ids'].items()}
    prefixes = [{'label': row['label'], 'raw': h.checked_bytes(row['prefix'])}
                for row in binding['prefixes']]
    result = count_prefixes(prefixes, registry, mapping['root_models'], frozen_ids,
                            mapping['excluded_roots'])
    result['binding'] = binding
    return result
