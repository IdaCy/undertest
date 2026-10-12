import copy
import hashlib
import json
import re


_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
_SHARED = ('context', 'context_canonical_sha256', 'frozen_config',
           'original_refs', 'native_job_sha256', 'original_seed', 'awareness_type')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _ids(values, name):
    if (not isinstance(values, (list, tuple))
            or any(not isinstance(value, str) or not value for value in values)
            or len(set(values)) != len(values)):
        raise ValueError(name)
    return set(values)


def _groups(jobs, questions, attempted_ids, ambiguous_ids):
    expected = _ids(questions, 'invalid_questions')
    if not expected or not isinstance(jobs, list) or not jobs:
        raise ValueError('jobs_and_questions_required')
    blocked = (_ids(attempted_ids, 'invalid_attempted_ids')
               | _ids(ambiguous_ids, 'invalid_ambiguous_ids'))
    groups, seen, definitions = {}, set(), {}
    namespace = None
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError('invalid_job')
        _encoded(job)
        parts = [job.get(key) for key in
                 ('family', 'model', 'run_id', 'stage', 'question_id')]
        if any(not isinstance(value, str) or not _COMPONENT.fullmatch(value)
               for value in parts):
            raise ValueError('invalid_measurement_components')
        family, model, run, stage, question = parts
        mid = '/'.join(parts)
        if job.get('measurement_id') != mid or mid in seen:
            raise ValueError('duplicate_or_changed_measurement')
        if mid in blocked:
            raise ValueError('attempted_or_ambiguous_measurement')
        if stage not in ('early', 'end') or question not in expected:
            raise ValueError('unexpected_stage_or_question')
        scope = (family, model, stage)
        if namespace is None:
            namespace = scope
        elif scope != namespace:
            raise ValueError('mixed_measurement_namespace')
        if any(key not in job for key in _SHARED):
            raise ValueError('missing_context_binding')
        common = _encoded({key: job[key] for key in _SHARED})
        context_id = '/'.join(parts[:4])
        group = groups.setdefault(context_id, {'binding': common, 'questions': []})
        if group['binding'] != common:
            raise ValueError('context_binding_changed')
        definition = _encoded({key: job[key] for key in
                               ('question_canonical_sha256', 'threshold', 'awareness_type')})
        if question in definitions and definitions[question] != definition:
            raise ValueError('question_definition_changed')
        definitions[question] = definition
        group['questions'].append(question)
        seen.add(mid)
    if any(group['questions'] != list(questions) for group in groups.values()):
        raise ValueError('incomplete_or_reordered_context_questions')
    return groups


def hashed_context_assignment(jobs, context_counts, *, salt, question_ids,
                              attempted_ids=(), ambiguous_ids=()):
    groups = _groups(jobs, question_ids, attempted_ids, ambiguous_ids)
    if (not isinstance(salt, str) or not salt
            or not isinstance(context_counts, dict) or not context_counts
            or any(not isinstance(owner, str) or not _COMPONENT.fullmatch(owner)
                   or type(count) is not int or count < 0
                   for owner, count in context_counts.items())
            or sum(context_counts.values()) != len(groups)):
        raise ValueError('invalid_context_counts_or_salt')
    ranked = sorted(groups, key=lambda context: (
        hashlib.sha256(_encoded(['supplement_context_assignment.v1', salt, context])).hexdigest(),
        context))
    assignment, start = {}, 0
    for owner, count in sorted(context_counts.items()):
        assignment.update({context: owner for context in ranked[start:start + count]})
        start += count
    return assignment


def partition_supplement_jobs(jobs, owner_by_context, *, question_ids,
                              attempted_ids=(), ambiguous_ids=()):
    groups = _groups(jobs, question_ids, attempted_ids, ambiguous_ids)
    if (not isinstance(owner_by_context, dict) or set(owner_by_context) != set(groups)
            or any(not isinstance(owner, str) or not _COMPONENT.fullmatch(owner)
                   for owner in owner_by_context.values())):
        raise ValueError('context_owner_membership_changed')
    partitions = {owner: {'jobs': [], 'context_ids': []}
                  for owner in sorted(set(owner_by_context.values()))}
    placed = set()
    for job in jobs:
        context = '/'.join(job[key] for key in ('family', 'model', 'run_id', 'stage'))
        part = partitions[owner_by_context[context]]
        part['jobs'].append(copy.deepcopy(job))
        if context not in placed:
            part['context_ids'].append(context)
            placed.add(context)
    return partitions
