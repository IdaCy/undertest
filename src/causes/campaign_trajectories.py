"""Assemble verified trajectory records from an explicit immutable input cut.

The caller verifies and selects the frozen assignments, receipt cut, reader
qualification, and quality dispositions. This module performs no discovery,
index capture, inference, release selection, output write, or network request.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Sequence

from causes import campaign_reader as reader
from causes import campaign_reader_freeze as freeze
from causes import campaign_reader_watch as watch
from causes.campaign_analysis import reader_label, self_label
from causes.campaign_reader_evidence import verify_reader_capture


@dataclass(frozen=True)
class FrozenModel:
    jobs: Sequence[dict]
    config: dict
    jobs_file_sha256: str
    config_file_sha256: str


@dataclass(frozen=True)
class ReaderArtifacts:
    """Exact retained pins; None declares absence in the supplied historical cut."""

    result: dict | None
    technical_missing: dict | None
    binding: dict | None


@dataclass(frozen=True)
class ReaderInputs:
    config: dict
    prompts: Path
    expected_source_sha256: str
    binding_pins: dict
    output_base: Path
    artifacts: Mapping[tuple[str, str, str], ReaderArtifacts]


def _require(value, message):
    if not value:
        raise ValueError(message)


def _relative_root(value):
    path = Path(value)
    _require(type(value) is str and value and not path.is_absolute()
             and str(path) == value and not any(p in ('.', '..') for p in path.parts),
             'explicit_canonical_output_root_required')
    return value


def select_reader_bound_receipts(
        actor_receipts: Sequence[dict], reader_receipts: Mapping[str, dict | None], *,
        bound_receipts: Mapping[str, dict | None]) -> dict:
    """Select a receipt cut without replacing the snapshots used by readers.

    Both mappings must explicitly name the same selected physical runs (receipt
    ``run`` values). Reader receipts come from the designated reader feed; bound
    receipts are the complete ``snapshot_receipt`` objects in retained bindings.
    None declares an absent feed receipt or binding in the supplied cut.

    A selected run with no reader receipt is reported missing even if an actor
    receipt exists. A retained binding requires an identical reader receipt.
    When both feeds contain a run, their summary hashes must agree and the
    reader receipt wins. Duplicate actor rows are refused as ambiguous input.

    Returned receipts are sorted by physical run, copied, and ready for
    assemble_trajectories. This pure selection step does not verify files,
    assignment membership, reader artifacts, or missingness dispositions.
    """
    _require(isinstance(actor_receipts, Sequence) and not isinstance(actor_receipts, (str, bytes)),
             'actor_receipt_sequence_required')
    _require(isinstance(reader_receipts, Mapping) and isinstance(bound_receipts, Mapping),
             'reader_receipt_mapping_required')
    _require(set(reader_receipts) == set(bound_receipts), 'reader_receipt_scope_mismatch')

    def checked_receipt(row, expected_run=None):
        _require(type(row) is dict and type(row.get('run')) is str
                 and type(row.get('summary_sha256')) is str and row['summary_sha256'],
                 'snapshot_receipt_identity_required')
        run = _relative_root(row['run'])
        _require(expected_run is None or run == expected_run, 'reader_receipt_run_mismatch')
        return run

    actors = {}
    for row in actor_receipts:
        run = checked_receipt(row)
        _require(run not in actors, 'duplicate_actor_receipt')
        actors[run] = row
    chosen = {run: row for run, row in actors.items() if run not in reader_receipts}
    missing, preferred = [], []
    for run in reader_receipts:
        _require(type(run) is str, 'reader_receipt_run_required')
        _relative_root(run)
        row, bound = reader_receipts[run], bound_receipts[run]
        if row is not None:
            checked_receipt(row, run)
        if bound is not None:
            checked_receipt(bound, run)
            _require(row is not None and reader.encoded(row) == reader.encoded(bound),
                     'reader_bound_receipt_mismatch')
        if row is None:
            missing.append(run)
            continue
        if run in actors:
            _require(actors[run]['summary_sha256'] == row['summary_sha256'],
                     'conflicting_snapshot_identity')
            preferred.append(run)
        chosen[run] = row
    return {'receipts': [copy.deepcopy(chosen[run]) for run in sorted(chosen)],
            'missing_reader_runs': sorted(missing), 'reader_preferred_runs': sorted(preferred)}


def _reader_json(reference, inputs):
    path = watch.confined(reference['path'], inputs.output_base)
    _require(type(reference['bytes']) is int and path.stat().st_size == reference['bytes'],
             'reader_artifact_size_mismatch')
    return watch.verified_json(path, reference['sha256'])


def reader_inputs_from_freeze(complete_reference, *, jobs_by_model, reader_config, prompts,
                              expected_reader_source_sha256, expected_freezer_source):
    """Convert pinned freeze metadata into assignment inputs and bound receipts.

    The caller accepts the freeze custody and adopts both source revisions.
    This reads metadata and bindings, not response or judgment bodies; semantic
    verification remains in assemble_trajectories. Explicit absences never
    discover later files. Both returned mappings use (model, run_id) keys.
    """
    _require(isinstance(jobs_by_model, Mapping) and jobs_by_model, 'frozen_reader_jobs_required')
    complete = json.loads(watch.checked_pin(complete_reference), object_pairs_hook=freeze.pairs)
    _require(complete['schema'] == 'causes.reader_artifact_freeze_complete.v1', 'reader_freeze_schema')
    _require(reader.encoded(complete['source']) == reader.encoded(expected_freezer_source),
             'reader_freezer_source_mismatch')
    directory = Path(complete_reference['path']).parent
    _require(Path(complete['reader_artifacts']['path']) == directory / 'READER_ARTIFACTS.json',
             'reader_freeze_artifacts_path_mismatch')
    rows = json.loads(watch.checked_pin(complete['reader_artifacts']), object_pairs_hook=freeze.pairs)
    _require(type(rows) is list, 'reader_freeze_artifact_list_required')
    expected, jobs = {}, {}
    for model, model_jobs in jobs_by_model.items():
        _require(isinstance(model_jobs, Sequence) and not isinstance(model_jobs, (str, bytes)),
                 'frozen_reader_job_sequence_required')
        for slot in freeze.expected_slots(model_jobs, model):
            expected[model, slot['run_id'], slot['stage']] = slot['frozen_job_sha256']
        jobs.update(((model, job['allocation']['run_id']), job) for job in model_jobs)
    _require(type(complete['slots']) is int and complete['slots'] == len(expected), 'reader_frozen_stage_cover')
    output, artifacts, bindings = directory / 'reader', {}, {}
    for item in rows:
        key = item['model'], item['run_id'], item['stage']
        _require(key in expected and key not in artifacts
                 and item['frozen_job_sha256'] == expected[key], 'frozen_reader_stage_identity_changed')
        assignment = key[:2]
        base = output / key[0] / key[1]
        for field, path in (('result', base / key[2] / 'RESULT.json'),
                            ('technical_missing', base / key[2] / 'TECHNICAL_MISSING.json'),
                            ('binding', base / 'BINDING.json')):
            reference = item[field]
            _require(reference is None or watch.confined(reference['path'], output) == path,
                     'reader_artifact_identity_path_mismatch')
        _require(item['result'] is None or item['technical_missing'] is None,
                 'reader_result_and_missing_conflict')
        _require(item['result'] is None and item['technical_missing'] is None or item['binding'] is not None,
                 'published_reader_stage_without_binding')
        _require(assignment not in bindings or reader.encoded(bindings[assignment]) == reader.encoded(item['binding']),
                 'reader_binding_changed_between_stages')
        bindings[assignment] = item['binding']
        artifacts[key] = ReaderArtifacts(**{field: copy.deepcopy(item[field])
                                            for field in ('result', 'technical_missing', 'binding')})
    _require(set(artifacts) == set(expected), 'reader_frozen_stage_cover')
    inputs, receipts = {}, {}
    for key, reference in bindings.items():
        current = ReaderInputs(copy.deepcopy(reader_config), Path(prompts), expected_reader_source_sha256,
                               {}, output, artifacts)
        binding = _reader_json(reference, current) if reference is not None else None
        if binding is not None:
            _require(binding['model'] == key[0] and binding['run_id'] == key[1]
                     and binding['frozen_job_sha256'] == reader.digest(jobs[key]), 'frozen_reader_binding_changed')
            _require(type(binding['reader_pins']) is dict and type(binding['snapshot_receipt']) is dict,
                     'frozen_reader_binding_metadata_required')
            current = ReaderInputs(current.config, current.prompts, current.expected_source_sha256,
                                   copy.deepcopy(binding['reader_pins']), output, artifacts)
        inputs[key] = current
        receipts[key] = copy.deepcopy(binding['snapshot_receipt']) if binding is not None else None
    return inputs, receipts


def merge_trajectory_outputs(groups, *, assignment_order):
    """Merge (assignments_by_model, native_output) pairs with exact coverage.

    Each group declares its own roster. Group rosters must be disjoint and
    cover assignment_order exactly. Records and issues keep native meanings;
    coverage lists follow the supplied whole-roster order. No files are read.
    """
    def roster(value):
        _require(isinstance(value, Mapping) and value, 'trajectory_group_roster_required')
        result = {}
        for model, ids in value.items():
            _require(freeze.safe_name(model) and isinstance(ids, Sequence) and not isinstance(ids, (str, bytes))
                     and all(freeze.safe_name(rid) for rid in ids) and len(ids) == len(set(ids)),
                     'invalid_or_duplicate_trajectory_assignment')
            result[model] = set(ids)
        return result

    whole = roster(assignment_order)
    _require(isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)), 'trajectory_groups_required')
    records, issues = {model: {} for model in whole}, []
    coverage = {model: {'unobserved': set(), 'quality_unavailable': set()} for model in whole}
    seen = {model: set() for model in whole}
    for assignments, value in groups:
        declared = roster(assignments)
        _require(set(declared) <= set(whole) and set(value) == {'records', 'issues', 'coverage'}
                 and set(value['records']) == set(declared) and set(value['coverage']) == set(declared)
                 and type(value['issues']) is list, 'native_trajectory_output_scope_mismatch')
        for model, ids in declared.items():
            _require(ids <= whole[model] and not ids.intersection(seen[model]), 'overlapping_or_unknown_trajectory_group')
            rows, missing = value['records'][model], value['coverage'][model]
            _require(isinstance(rows, Mapping) and set(missing) == {'unobserved', 'quality_unavailable'},
                     'native_trajectory_coverage_shape')
            for rid, record in rows.items():
                _require(rid in ids and record['model'] == model and record['run_id'] == rid,
                         'native_trajectory_record_identity')
            parts = [set(rows)]
            for kind, run_ids in missing.items():
                _require(type(run_ids) is list and all(type(rid) is str for rid in run_ids)
                         and len(run_ids) == len(set(run_ids)) and set(run_ids) <= ids,
                         'duplicate_or_unknown_native_coverage')
                parts.append(set(run_ids))
            _require(sum(map(len, parts)) == len(ids) and set().union(*parts) == ids,
                     'disjoint_complete_native_coverage_required')
            records[model].update(copy.deepcopy(rows))
            for kind, run_ids in missing.items():
                coverage[model][kind].update(run_ids)
            seen[model].update(ids)
        issues.extend(copy.deepcopy(value['issues']))
    _require(seen == whole, 'whole_trajectory_roster_cover_required')
    return {'records': {model: {rid: records[model][rid] for rid in ids if rid in records[model]}
                        for model, ids in assignment_order.items()},
            'issues': issues,
            'coverage': {model: {kind: [rid for rid in ids if rid in members]
                                  for kind, members in coverage[model].items()}
                         for model, ids in assignment_order.items()}}


def _contexts(job, loaded, binding):
    summary = loaded['summary.json']
    contexts, sources = {}, {}
    for stage in ('early', 'end'):
        name = stage + '_context.json'
        if name not in loaded or summary.get(stage + '_workspace_status') != 'captured':
            continue
        if stage == 'end' and (not job['allocation']['continue_to_end']
                               or summary['end_status'] != 'natural_completion'):
            continue
        _require(stage != 'end' or 'early' in contexts, 'paired_context_mismatch')
        messages, _, _, _ = watch.stage_context(stage, loaded)
        contexts[stage] = messages
        sources[stage] = {'path': str(Path(binding['snapshot_receipt']['path']) / name),
                          'bytes_sha256': binding['source_files'][name]['sha256'],
                          'context_sha256': reader.digest(messages)}
    return contexts, sources


def _self_measurements(summary, contexts, sources, config):
    measured = {stage: {} for stage in ('early', 'end')}
    for probe in summary.get('probes', []):
        stage, instrument = probe['stage'], probe['instrument']
        _require(stage in measured and instrument not in measured[stage], 'duplicate_or_unknown_probe_stage')
        if stage not in contexts or probe.get('context_sha256') != sources[stage]['context_sha256']:
            measured[stage][instrument] = {'label': None, 'technical_status': 'missing_or_mismatched_context'}
            continue
        questions = [q for q in config['questions'] if q['id'] == instrument]
        _require(len(questions) == 1 and probe.get('question_sha256') == reader.digest(questions[0]),
                 'question_identity_mismatch')
        measured[stage][instrument] = {'label': self_label(probe), 'technical_status': probe['technical_status'],
                                       'reason': probe.get('reason'), 'context_sha256': probe.get('context_sha256')}
    return measured


def _reader_measurements(model, run_id, job, config, snapshot, loaded, contexts, inputs, awareness_types,
                         expected_binding):
    measured, dispositions, issues = {}, {}, []
    for stage in job['allocation']['reader_stages']:
        _require(stage in ('early', 'end'), 'unknown_reader_stage')
        key = (model, run_id, stage)
        _require(key in inputs.artifacts and isinstance(inputs.artifacts[key], ReaderArtifacts),
                 'explicit_reader_artifact_or_absence_required')
        artifacts = inputs.artifacts[key]
        assignment_path = inputs.output_base / model / run_id
        for reference, expected_path in (
                (artifacts.result, assignment_path / stage / 'RESULT.json'),
                (artifacts.technical_missing, assignment_path / stage / 'TECHNICAL_MISSING.json'),
                (artifacts.binding, assignment_path / 'BINDING.json')):
            _require(reference is None or Path(reference['path']) == expected_path,
                     'reader_artifact_identity_path_mismatch')
        if stage not in contexts:
            dispositions[stage] = 'final_actor_stage_unavailable'
        elif artifacts.technical_missing is not None:
            _require(artifacts.binding is not None, 'reader_missing_binding_required')
            missing = _reader_json(artifacts.technical_missing, inputs)
            binding = _reader_json(artifacts.binding, inputs)
            _require(missing.get('run_id') == run_id and missing.get('stage') == stage
                     and missing.get('status') == 'technical_missing' and missing.get('admissible_labels') is None
                     and missing.get('replayed') is False and missing.get('binding_sha256') == reader.digest(binding)
                     and binding.get('frozen_job_sha256') == reader.digest(job)
                     and binding.get('frozen_model_sha256') == reader.digest(config)
                     and binding.get('snapshot_receipt', {}).get('summary_sha256') == snapshot['summary_sha256'],
                     'reader_missing_disposition_binding_mismatch')
            dispositions[stage] = 'final_reader_technical_missing'
        else:
            dispositions[stage] = 'pending_reader'
        if expected_binding.get('endpoint_transition') is not None and (
                artifacts.result is not None or artifacts.technical_missing is not None):
            _require(artifacts.binding is not None, 'endpoint_reader_binding_required')
            retained_binding = _reader_json(artifacts.binding, inputs)
            _require(retained_binding.get('endpoint_transition') == expected_binding['endpoint_transition']
                     and retained_binding.get('frozen_job_sha256') == reader.digest(job)
                     and retained_binding.get('frozen_model_sha256') == reader.digest(config)
                     and retained_binding.get('snapshot_receipt') == snapshot,
                     'endpoint_reader_binding_mismatch')
        if artifacts.result is None:
            continue
        _reader_json(artifacts.result, inputs)
        if stage not in contexts:
            issues.append({'run_id': run_id, 'stage': stage, 'issue': 'reader_unusable_context'})
            continue
        result_path = Path(artifacts.result['path'])
        _require(result_path.name == 'RESULT.json', 'reader_result_filename_mismatch')
        result = verify_reader_capture(result_path.parent, contexts[stage], loaded['initial_context.json'],
            'prefix' if stage == 'early' else 'full', inputs.config, inputs.prompts,
            inputs.expected_source_sha256, prefix_context=contexts['early'] if stage == 'end' else None)
        _reader_json(artifacts.result, inputs)
        measured[stage] = {'path': str(result_path), 'result_sha256': artifacts.result['sha256'], 'instruments': {}}
        dispositions[stage] = 'final_verified_reader_result'
        for instrument, judgments in result['instruments'].items():
            measured[stage]['instruments'][instrument] = {'status': judgments['status'],
                'labels': {awareness: reader_label(judgments, awareness) for awareness in awareness_types}}
    return measured, dispositions, issues


def assemble_trajectories(receipts: Sequence[dict], models: Mapping[str, FrozenModel], *,
                          registered_roots: Mapping[str, Sequence[str]], snapshots_root: Path,
                          reader_inputs: ReaderInputs, awareness_types: Sequence[str],
                          quality_held: Mapping[str, Mapping[str, dict]] | None = None,
                          quality_unavailable: Mapping[str, Sequence[str]] | None = None,
                          endpoint_transitions: Mapping[str, dict] | None = None,
                          reader_inputs_by_assignment: Mapping[tuple[str, str], ReaderInputs] | None = None):
    """Return records/issues and separate unobserved/quality-unavailable coverage.

    Quality annotations and unavailability are supplied validated facts, never
    inferred here. Missing actor or reader results remain missing. Reader pins
    and explicit None values bind the input cut even if later files now exist.
    Assignment overrides select a complete reader namespace, including its pins;
    absent stages never fall back to another namespace.
    """
    _require(models and set(models) == set(registered_roots), 'model_root_scope_mismatch')
    _require(awareness_types and len(set(awareness_types)) == len(awareness_types)
             and all(type(value) is str and value in reader.TYPES for value in awareness_types),
             'awareness_roster_required')
    held, unavailable = quality_held or {}, quality_unavailable or {}
    _require(set(held) <= set(models) and set(unavailable) <= set(models), 'quality_model_scope_mismatch')
    jobs, root_models = {}, {}
    for model, frozen in models.items():
        _require(type(model) is str and model and Path(model).name == model, 'invalid_model_key')
        jobs[model] = {}
        for job in frozen.jobs:
            rid = job['allocation']['run_id']
            _require(type(rid) is str and rid and rid not in ('.', '..') and Path(rid).name == rid
                     and rid not in jobs[model], 'duplicate_or_invalid_frozen_assignment')
            jobs[model][rid] = job
        _require(registered_roots[model], 'registered_output_root_required')
        for value in registered_roots[model]:
            root = _relative_root(value)
            _require(root not in root_models, 'output_root_has_multiple_owners')
            root_models[root] = model
        _require(set(held.get(model, {})) <= set(jobs[model])
                 and set(unavailable.get(model, ())) <= set(jobs[model]), 'quality_assignment_scope_mismatch')
    reader_overrides = {} if reader_inputs_by_assignment is None else reader_inputs_by_assignment
    _require(isinstance(reader_overrides, Mapping), 'reader_override_mapping_required')
    _require(isinstance(reader_inputs, ReaderInputs), 'reader_inputs_required')
    for key, value in reader_overrides.items():
        _require(isinstance(key, tuple) and len(key) == 2
                 and key[0] in jobs and key[1] in jobs[key[0]],
                 'reader_override_assignment_unknown')
        _require(isinstance(value, ReaderInputs), 'reader_override_inputs_required')
    snapshots = {}
    for row in receipts:
        root = str(Path(row['run']).parent)
        if root not in root_models:
            continue
        if row['run'] in snapshots:
            _require(snapshots[row['run']]['summary_sha256'] == row['summary_sha256'], 'conflicting_snapshot_identity')
        snapshots[row['run']] = row
    records, issues = {model: {} for model in models}, []
    for run, snapshot in sorted(snapshots.items()):
        model = root_models[str(Path(run).parent)];run_id = Path(run).name
        if run_id not in jobs[model]:
            continue
        _require(run_id not in records[model], 'physical_assignment_present_in_multiple_output_roots')
        frozen = models[model];job = jobs[model][run_id]
        assignment_reader = reader_overrides.get((model, run_id), reader_inputs)
        selected = {'model': model, 'job': job, 'config': frozen.config,
                    'jobs_file_sha256': frozen.jobs_file_sha256, 'model_file_sha256': frozen.config_file_sha256}
        if endpoint_transitions is not None:
            selected['endpoint_transitions'] = endpoint_transitions
        binding, loaded = watch.bind_snapshot(snapshot, selected, snapshots_root, assignment_reader.binding_pins)
        captured_config = frozen.config
        if 'endpoint_transition' in binding:
            captured_config, _ = watch.endpoint_transition.snapshot_config(
                endpoint_transitions[str(Path(run).parent)], root=str(Path(run).parent), job=job,
                original_config=frozen.config, original_file_sha256=frozen.config_file_sha256)
        # The inherited binder compares Python values. Preserve numeric type as
        # part of frozen allocation/spec/variant/config identity as well.
        _require(reader.encoded([loaded[name + '.json'] for name in ('allocation', 'spec', 'variant', 'model')])
                 == reader.encoded([job['allocation'], job['spec'], job['variant'], captured_config]),
                 'typed_frozen_identity_mismatch')
        summary = loaded['summary.json'];contexts, sources = _contexts(job, loaded, binding)
        measured_self = _self_measurements(summary, contexts, sources, frozen.config)
        measured_reader, dispositions, found = _reader_measurements(model, run_id, job, frozen.config,
            snapshot, loaded, contexts, assignment_reader, awareness_types, binding)
        issues.extend(found)
        record = {'run_id': run_id, 'model': model, 'task_id': job['spec']['task_id'],
            'variant_id': job['variant']['id'], 'allocation': copy.deepcopy(job['allocation']), 'source': str(Path(snapshot['path'])),
            'summary_sha256': snapshot['summary_sha256'], 'snapshot_manifest': str(Path(snapshot['manifest'])),
            'end_status': summary['end_status'], 'early_status': summary['early_status'],
            'actor_calls': summary['calls'], 'tool_results': summary['tool_results'],
            'context_sources': sources, 'self': measured_self, 'reader': measured_reader, 'reader_dispositions': dispositions}
        if 'endpoint_transition' in binding:
            record['endpoint_transition'] = copy.deepcopy(binding['endpoint_transition'])
            record['frozen_model_sha256'] = reader.digest(frozen.config)
        _require(run_id not in set(unavailable.get(model, ())), 'quality_unissued_disposition_conflicts_with_observed_episode')
        if run_id in held.get(model, {}):
            record['intervention_quality'] = copy.deepcopy(held[model][run_id])
        records[model][run_id] = record
    coverage = {model: {'unobserved': [rid for rid in jobs[model] if rid not in records[model]
                                      and rid not in set(unavailable.get(model, ()))],
                       'quality_unavailable': [rid for rid in jobs[model] if rid in set(unavailable.get(model, ()))]}
                for model in models}
    return {'records': records, 'issues': issues, 'coverage': coverage}
