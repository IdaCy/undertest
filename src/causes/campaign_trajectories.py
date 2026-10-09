"""Assemble verified trajectory records from an explicit immutable input cut.

The caller verifies and selects the frozen assignments, receipt cut, reader
qualification, and quality dispositions. This module performs no discovery,
index capture, inference, release selection, output write, or network request.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from causes import campaign_reader as reader
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


def _reader_json(reference, inputs):
    path = watch.confined(reference['path'], inputs.output_base)
    _require(type(reference['bytes']) is int and path.stat().st_size == reference['bytes'],
             'reader_artifact_size_mismatch')
    return watch.verified_json(path, reference['sha256'])


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


def _reader_measurements(model, run_id, job, config, snapshot, loaded, contexts, inputs, awareness_types):
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
                          quality_unavailable: Mapping[str, Sequence[str]] | None = None):
    """Return records/issues and separate unobserved/quality-unavailable coverage.

    Quality annotations and unavailability are supplied validated facts, never
    inferred here. Missing actor or reader results remain missing. Reader pins
    and explicit None values bind the input cut even if later files now exist.
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
        selected = {'model': model, 'job': job, 'config': frozen.config,
                    'jobs_file_sha256': frozen.jobs_file_sha256, 'model_file_sha256': frozen.config_file_sha256}
        binding, loaded = watch.bind_snapshot(snapshot, selected, snapshots_root, reader_inputs.binding_pins)
        # The inherited binder compares Python values. Preserve numeric type as
        # part of frozen allocation/spec/variant/config identity as well.
        _require(reader.encoded([loaded[name + '.json'] for name in ('allocation', 'spec', 'variant', 'model')])
                 == reader.encoded([job['allocation'], job['spec'], job['variant'], frozen.config]),
                 'typed_frozen_identity_mismatch')
        summary = loaded['summary.json'];contexts, sources = _contexts(job, loaded, binding)
        measured_self = _self_measurements(summary, contexts, sources, frozen.config)
        measured_reader, dispositions, found = _reader_measurements(model, run_id, job, frozen.config,
            snapshot, loaded, contexts, reader_inputs, awareness_types)
        issues.extend(found)
        record = {'run_id': run_id, 'model': model, 'task_id': job['spec']['task_id'],
            'variant_id': job['variant']['id'], 'allocation': copy.deepcopy(job['allocation']), 'source': str(Path(snapshot['path'])),
            'summary_sha256': snapshot['summary_sha256'], 'snapshot_manifest': str(Path(snapshot['manifest'])),
            'end_status': summary['end_status'], 'early_status': summary['early_status'],
            'actor_calls': summary['calls'], 'tool_results': summary['tool_results'],
            'context_sources': sources, 'self': measured_self, 'reader': measured_reader, 'reader_dispositions': dispositions}
        _require(run_id not in set(unavailable.get(model, ())), 'quality_unissued_disposition_conflicts_with_observed_episode')
        if run_id in held.get(model, {}):
            record['intervention_quality'] = copy.deepcopy(held[model][run_id])
        records[model][run_id] = record
    coverage = {model: {'unobserved': [rid for rid in jobs[model] if rid not in records[model]
                                      and rid not in set(unavailable.get(model, ()))],
                       'quality_unavailable': [rid for rid in jobs[model] if rid in set(unavailable.get(model, ()))]}
                for model in models}
    return {'records': records, 'issues': issues, 'coverage': coverage}
