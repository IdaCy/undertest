import copy
import hashlib
import json
from pathlib import Path, PurePosixPath


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def relative(value):
    require(type(value) is str and value and '\x00' not in value,
            'invalid_registry_path')
    path = PurePosixPath(value)
    require(path.parts and not path.is_absolute() and str(path) == value
            and '..' not in path.parts, 'noncanonical_registry_path')
    return path


def distinct_roots(values):
    require(type(values) is list and len(set(values)) == len(values),
            'distinct_registry_roots_required')
    for value in values:
        relative(value)
    return set(values)


def validate_registry(value):
    require(value['schema'] == 'causes.output_root_registry.v1', 'root_registry_schema')
    fixed = value['fixed_roots']
    require(type(fixed) is dict and fixed, 'fixed_registry_models_required')
    assigned_roots = set()
    for model, roots in fixed.items():
        require(type(model) is str and relative(model).name == model,
                'invalid_registry_model')
        current = distinct_roots(roots)
        require(current and not current & assigned_roots, 'distinct_fixed_registry_roots_required')
        for root in current:
            path = relative(root)
            require(len(path.parts) == 3 and path.parts[0] == 'runs'
                    and path.parts[-1] == model, 'fixed_registry_root_model_mismatch')
        assigned_roots.update(current)
    corpus = distinct_roots(value['corpus_roots'])
    sync = distinct_roots(value['sync_roots'])
    require(not corpus & assigned_roots, 'corpus_root_in_fixed_registry')
    require(assigned_roots | corpus <= sync, 'registry_root_missing_from_sync')
    return copy.deepcopy(value)


def checked_registry(path, expected_sha256):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)),
            'noncanonical_registry_file')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'root_registry_pin_changed')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'duplicate_registry_key')
            result[key] = item
        return result
    return validate_registry(json.loads(raw, object_pairs_hook=unique))


def extend_registry(previous, fixed_additions, corpus_additions):
    result = validate_registry(previous)
    require(set(fixed_additions) <= set(result['fixed_roots']),
            'new_fixed_model_not_authorized')
    additions = []
    for model, roots in fixed_additions.items():
        distinct_roots(roots)
        result['fixed_roots'][model].extend(roots)
        additions.extend(roots)
    distinct_roots(corpus_additions)
    result['corpus_roots'].extend(corpus_additions)
    additions.extend(corpus_additions)
    require(len(set(additions)) == len(additions)
            and not set(additions) & set(previous['sync_roots']),
            'registry_addition_already_registered')
    result['sync_roots'].extend(additions)
    return validate_registry(result)


def fixed_rows(rows, frozen_ids_by_model, registry):
    registry = validate_registry(registry)
    require(set(frozen_ids_by_model) == set(registry['fixed_roots']),
            'exact_frozen_registry_models_required')
    owners, result = {}, {}
    for model, values in frozen_ids_by_model.items():
        require(not isinstance(values, (str, bytes)), 'explicit_frozen_ID_collection_required')
        values = list(values)
        require(len(set(values)) == len(values), 'duplicate_frozen_registry_ID')
        for run_id in values:
            require(relative(run_id).name == run_id, 'noncanonical_frozen_registry_ID')
            require(run_id not in owners, 'frozen_ID_shared_between_models')
            owners[run_id] = model
        result[model] = {}
    for row in rows:
        path = relative(row['run'])
        model = owners.get(path.name)
        if model is None:
            continue
        require(str(path.parent) in registry['fixed_roots'][model],
                'original_ID_in_unregistered_root')
        require(path.name not in result[model], 'multiple_custody_rows_for_original_ID')
        result[model][path.name] = copy.deepcopy(row)
    return result


def reader_candidates(rows, selected, registry):
    registry = validate_registry(registry)
    frozen = {model: [] for model in registry['fixed_roots']}
    for run_id, selection in selected.items():
        require(selection['model'] in frozen, 'reader_model_not_registered')
        require(selection['job']['allocation']['run_id'] == run_id,
                'reader_selection_ID_mismatch')
        frozen[selection['model']].append(run_id)
    joined = fixed_rows(rows, frozen, registry)
    return [row for model in joined for row in joined[model].values()]
