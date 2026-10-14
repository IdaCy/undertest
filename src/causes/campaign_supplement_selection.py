"""Resolve exact saved job selections without copying large request inventories.

This read-only adapter grants no execution authority. The caller must establish
closed ownership, reconcile every occupied attempt, retain route failure history,
and run each returned family/model group in its own native queue namespace.
"""
import json

from causes import campaign_supplement as core


SCHEMA = 'causes.supplement_saved_job_selection.v1'


def _require(condition, reason):
    if not condition:
        raise core.IntegrityError(reason)


def _scope(job):
    _require(type(job) is dict, 'selection_job_object')
    fields = ('family', 'model', 'run_id', 'stage', 'question_id')
    _require(all(field in job for field in fields), 'selection_job_identity_fields')
    for field in fields:
        core.namespace_component(job[field])
    _require(job['stage'] in ('early', 'end'), 'selection_job_stage')
    _require(job.get('measurement_id') == '/'.join(job[field] for field in fields),
             'selection_job_identity')
    return job['family'], job['model']


def load_selected_groups(reference, *, occupied_ids=()):
    """Return original jobs, grouped by scope in first-seen source order.

    Each source pins a JSON job list and a selection with the same ``jobs`` pin
    and ``rows`` containing original ordinals, measurement IDs and input hashes.
    The selection is explicit: no failed, completed, active or ambiguous attempt
    may be selected. Occupied IDs cause refusal rather than silent filtering.
    This does not attest that the caller's supplied occupied set is complete.
    """
    _require(not isinstance(occupied_ids, (str, bytes, dict)),
             'selection_occupied_ids_must_be_collection')
    manifest = json.loads(core.pinned(reference))
    _require(type(manifest) is dict and manifest.get('schema') == SCHEMA,
             'selection_manifest_schema')
    sources = manifest.get('sources')
    _require(type(sources) is list and sources, 'selection_sources')
    occupied = list(occupied_ids)
    _require(all(type(mid) is str and mid for mid in occupied),
             'selection_occupied_ids')
    occupied = set(occupied)
    groups, selected_ids, source_paths = {}, set(), set()
    for source in sources:
        _require(type(source) is dict and set(source) == {'jobs', 'selection'},
                 'selection_source_descriptor')
        selection = json.loads(core.pinned(source['selection']))
        _require(type(selection) is dict and selection.get('jobs') == source['jobs'],
                 'selection_source_binding')
        rows = selection.get('rows')
        _require(type(rows) is list and rows, 'selection_rows')
        jobs = json.loads(core.pinned(source['jobs']))
        _require(type(jobs) is list and jobs, 'selection_original_jobs')
        path = source['jobs']['path']
        _require(path not in source_paths, 'selection_repeated_source')
        source_paths.add(path)
        all_ids, source_scope = set(), None
        for job in jobs:
            scope = _scope(job)
            _require(source_scope in (None, scope), 'selection_mixed_source_scope')
            source_scope = scope
            _require(job['measurement_id'] not in all_ids,
                     'selection_duplicate_original_id')
            all_ids.add(job['measurement_id'])
        previous = -1
        for row in rows:
            _require(type(row) is dict, 'selection_row_object')
            ordinal = row.get('ordinal')
            _require(type(ordinal) is int and previous < ordinal < len(jobs),
                     'selection_original_ordinal_order')
            previous = ordinal
            job = jobs[ordinal]
            mid = job['measurement_id']
            _require(row.get('measurement_id') == mid and
                     row.get('input_sha256') == core.digest(job),
                     'selection_original_job_changed')
            _require(mid not in occupied, 'selection_occupied_attempt')
            _require(mid not in selected_ids, 'selection_duplicate_selected_id')
            selected_ids.add(mid)
            if source_scope not in groups:
                groups[source_scope] = {
                    'family': source_scope[0], 'model': source_scope[1], 'jobs': []}
            groups[source_scope]['jobs'].append(job)
    return list(groups.values())
