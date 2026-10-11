from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from . import campaign_analysis as analysis
from . import campaign_reader as reader
from . import campaign_reader_watch as watch
from . import campaign_trajectories as trajectories
from .task_cluster_intervals import summarize_task_bounds


def checked(reference):
    raw = Path(reference['path']).read_bytes()
    if len(raw) != reference['bytes'] or hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise ValueError('changed_input_pin')
    return json.loads(raw)


def extract_prefix_measurements(roster, arms, *, expected_pairs, expected_tasks, model):
    pairs = roster['pairs']
    if (len(pairs) != expected_pairs or len({p['pair_id'] for p in pairs}) != expected_pairs
            or len({p['task_id'] for p in pairs}) != expected_tasks
            or not arms or len(set(arms)) != len(arms) or not isinstance(model, str) or not model):
        raise ValueError('frozen_roster_membership_mismatch')
    cache, jobs_cache, seen = {}, {}, set()
    def load(ref):
        key = (ref['path'], ref['bytes'], ref['sha256'])
        if key not in cache:
            cache[key] = checked(ref)
        return cache[key]
    # Manifest/VERIFIED/config/job documents are immutable and shared across runs.
    rows, failures = [], []
    for pair in pairs:
        for arm in arms:
            member = pair['members'][arm]
            run_id = member['run_id']
            if run_id in seen:
                raise ValueError('duplicate_physical_draw')
            seen.add(run_id)
            row = {k: pair[k] for k in ('pair_id', 'task_id', 'seed', 'repetition', 'historical_parent_id')}
            row.update(arm=arm, run_id=run_id, label=None, technical_status='not_extracted')
            try:
                ref = member['frozen_jobs']
                key = (ref['path'], ref['bytes'], ref['sha256'])
                if key not in jobs_cache:
                    jobs = load(ref)
                    jobs = jobs['jobs'] if isinstance(jobs, dict) else jobs
                    jobs_cache[key] = {j['allocation']['run_id']: j for j in jobs}
                    if len(jobs_cache[key]) != len(jobs):
                        raise ValueError('duplicate_job_identity')
                job = jobs_cache[key][run_id]
                config = load(member['frozen_config'])
                allocation = job['allocation']
                if (reader.digest(job) != member['job_object_sha256']
                        or allocation['continue_to_end'] is not False
                        or allocation['seed'] != pair['seed']
                        or allocation['repetition'] != pair['repetition']
                        or job['spec']['task_id'] != pair['task_id']
                        or allocation['paired_baseline']['original_run_id'] != pair['historical_parent_id']
                        or allocation['system_prompt_intervention']['pair_id'] != pair['pair_id']
                        or allocation['system_prompt_intervention']['arm']['id'] != arm
                        or 'evaluation_presence' not in allocation['instruments']):
                    raise ValueError('frozen_pair_identity_mismatch')
                receipt = member['receipt']
                if receipt not in load(member['native_verified'])['runs']:
                    raise ValueError('exact_native_receipt_missing')
                manifest = load(member['manifest'])
                matched = [r for r in manifest['runs'] if r['run'] == receipt['run']]
                if len(matched) != 1 or member['manifest']['sha256'] != receipt['manifest_sha256']:
                    raise ValueError('manifest_identity_mismatch')
                frozen = dict(job=job, config=config, model=model,
                              jobs_file_sha256=ref['sha256'], model_file_sha256=member['frozen_config']['sha256'])
                binding, loaded = watch.bind_snapshot(receipt, frozen, Path(receipt['manifest']).parent, {})
                if set(member['files']) != set(binding['source_files']):
                    raise ValueError('roster_file_membership_mismatch')
                for name, actual in binding['source_files'].items():
                    named = member['files'][name]
                    if (named['path'] != str(Path(receipt['path']) / name)
                            or named['bytes'] != actual['bytes'] or named['sha256'] != actual['sha256']):
                        raise ValueError('roster_file_provenance_mismatch')
                contexts, sources = trajectories._contexts(job, loaded, binding)
                measured = trajectories._self_measurements(loaded['summary.json'], contexts, sources, config)
                value = measured['early'].get('evaluation_presence', {'label': None, 'technical_status': 'absent_probe'})
                row.update(label=value['label'], technical_status=value['technical_status'],
                           context_sha256=value.get('context_sha256'), binding=binding,
                           question_sha256=reader.digest(next(q for q in config['questions']
                                                              if q['id'] == 'evaluation_presence')),
                           probe_indices=[i for i, p in enumerate(loaded['summary.json'].get('probes', []))
                                          if p.get('stage') == 'early' and p.get('instrument') == 'evaluation_presence'],
                           summary=member['files']['summary.json'], receipt=receipt,
                           configuration=member['frozen_config'], jobs=ref)
            except Exception as exc:
                row.update(technical_status='validation_refused', error={'type': type(exc).__name__, 'message': str(exc)})
                failures.append({'run_id': run_id, 'arm': arm, 'error': row['error']})
            rows.append(row)
    return {'measurements': rows, 'validation_failures': failures, 'assigned_pairs': expected_pairs,
            'tasks': expected_tasks, 'arms': list(arms), 'model_calls': 0, 'host_calls': 0}


def contrast_readout(measurements, treatment, control, *, expected_pairs, expected_tasks,
                     infer=False, alpha=.05, bootstrap_draws=20000, seed=20261011):
    selected = [r for r in measurements if r['arm'] in (treatment, control)]
    by_pair, members, labels = {}, defaultdict(lambda: {'treatment': [], 'control': []}), {}
    for row in selected:
        if row['run_id'] in labels or row['technical_status'] == 'validation_refused':
            raise ValueError('invalid_measurement_membership')
        if row['label'] != analysis.self_label({'answer': row['label'],
                                                'technical_status': row['technical_status']}):
            raise ValueError('inconsistent_label_status')
        pair = by_pair.setdefault(row['pair_id'], {})
        if row['arm'] in pair:
            raise ValueError('duplicate_pair_arm')
        pair[row['arm']] = row
        labels[row['run_id']] = row['label']
        members[row['task_id']]['treatment' if row['arm'] == treatment else 'control'].append(row['run_id'])
    if len(by_pair) != expected_pairs or len(members) != expected_tasks:
        raise ValueError('incomplete_assigned_roster')
    transitions, complete_by_task, complete = Counter(), Counter(), 0
    for pair in by_pair.values():
        if set(pair) != {treatment, control}:
            raise ValueError('missing_assigned_pair_member')
        left, right = pair[control], pair[treatment]
        if any(left[k] != right[k] for k in ('task_id', 'seed', 'repetition', 'historical_parent_id')):
            raise ValueError('paired_identity_changed')
        transitions[(left['label'], right['label'])] += 1
        known = left['label'] is not None and right['label'] is not None
        complete += known
        complete_by_task[left['task_id']] += known
    descriptive = analysis.contrast({'factor': treatment + '_minus_' + control, 'members': dict(members)},
                                    labels, release=False, family_size=1)
    # Retain descriptive output only; the inherited replicate-level SE is inapplicable.
    descriptive = {k: descriptive[k] for k in (
        'factor', 'task_count', 'assigned', 'observed', 'all_assigned_observed',
        'equal_task_effect', 'category_equal_task_weights', 'cells',
        'missing_assigned_sample_effect_bounds')}
    bounds = {task: (cell['treatment']['assigned_probability_bounds'][0] - cell['control']['assigned_probability_bounds'][1],
                     cell['treatment']['assigned_probability_bounds'][1] - cell['control']['assigned_probability_bounds'][0])
              for task, cell in descriptive['cells'].items()}
    inference = summarize_task_bounds(bounds, alpha=alpha, bootstrap_draws=bootstrap_draws, seed=seed) if infer else None
    return {'treatment': treatment, 'control': control, 'assigned_pairs': expected_pairs,
            'observed_complete_pairs': complete, 'descriptive': descriptive,
            'complete_pairs_by_task': dict(complete_by_task),
            'paired_categories': [{'control': a, 'treatment': b, 'count': transitions[(a, b)]}
                                  for a in (*analysis.SELF_LABELS, None)
                                  for b in (*analysis.SELF_LABELS, None)],
            'task_bounds': bounds, 'inference': inference}
