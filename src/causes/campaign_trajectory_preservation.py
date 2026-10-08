"""Compare frozen trajectory records after their source evidence has been validated.

Callers supply verified custody aliases; this comparison does not validate new records or raw evidence.
"""

import copy
import json


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def same(left, right):
    return encoded(left) == encoded(right)


def preserve_prior(old, actual, aliases):
    """Compare validated records, permitting new IDs and pending reader-stage resolutions."""
    additions, conflicts = [], []
    for rid, previous in old.items():
        if rid not in actual:
            conflicts.append({'run_id': rid, 'field': 'missing_prior_identity'})
            continue
        current = copy.deepcopy(actual[rid])
        for stage, row in current['reader'].items():
            original = aliases.get(row['path'])
            if original is None or original['sha256'] != row['result_sha256']:
                conflicts.append({'run_id': rid, 'field': 'reader.' + stage + '.unproved_path_alias'})
            else:
                row['path'] = original['path']
        for field in set(previous) | set(current):
            if field not in ('reader', 'reader_dispositions') and (field not in previous or field not in current
                    or not same(previous[field], current[field])):
                conflicts.append({'run_id': rid, 'field': field})
        for stage, value in previous['reader'].items():
            if not same(value, current['reader'].get(stage)):
                conflicts.append({'run_id': rid, 'field': 'reader.' + stage})
        prior_dispositions, current_dispositions = previous['reader_dispositions'], current['reader_dispositions']
        for stage in set(prior_dispositions) | set(current_dispositions):
            before, after = prior_dispositions.get(stage), current_dispositions.get(stage)
            if before != after:
                if (stage in previous['allocation']['reader_stages'] and before in (None, 'pending_reader')
                        and after in ('pending_reader', 'final_verified_reader_result', 'final_reader_technical_missing')
                        and stage not in previous['reader']):
                    additions.append({'run_id': rid, 'stage': stage, 'before': before, 'after': after})
                else:
                    conflicts.append({'run_id': rid, 'field': 'reader_dispositions.' + stage})
        for stage in set(current['reader']) - set(previous['reader']):
            if (stage not in previous['allocation']['reader_stages']
                    or prior_dispositions.get(stage) not in (None, 'pending_reader')
                    or current_dispositions.get(stage) != 'final_verified_reader_result'):
                conflicts.append({'run_id': rid, 'field': 'reader.' + stage + '.unapproved_addition'})
    return {'prior_rows': len(old), 'all_prior_fields_preserved': not conflicts,
            'monotone_reader_additions': additions, 'conflicts': conflicts,
            'new_ids': sorted(set(actual) - set(old))}
