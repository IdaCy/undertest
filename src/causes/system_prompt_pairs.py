"""Build paired system-prompt interventions from frozen natural-baseline jobs."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _identifier(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value)


def build_pairs(jobs, config, arms, *, namespace, expected_model, expected_arm_count):
    """Return new jobs/configs without changing or filtering originals by outcomes.

    Each intervention reuses its original sampling seed intentionally. The
    original allocation is retained as provenance, including study membership.
    """
    if not _identifier(namespace, 48):
        raise ValueError('invalid_namespace')
    if not isinstance(config, dict) or not isinstance(expected_model, str) or not expected_model:
        raise ValueError('invalid_model_configuration')
    if config.get('model') != expected_model or not isinstance(config.get('system'), str):
        raise ValueError('model_or_system_mismatch')
    if type(expected_arm_count) is not int or expected_arm_count < 1:
        raise ValueError('invalid_arm_count')
    if not isinstance(arms, list) or len(arms) != expected_arm_count:
        raise ValueError('arm_count_mismatch')
    arm_ids, texts = set(), set()
    for arm in arms:
        if not isinstance(arm, dict) or set(arm) - {'id', 'text', 'primary'}:
            raise ValueError('invalid_arm')
        arm_id, text = arm.get('id'), arm.get('text')
        if not _identifier(arm_id, 24) or arm_id in arm_ids:
            raise ValueError('duplicate_or_invalid_arm_id')
        if not isinstance(text, str) or not text or text.strip() != text or '\0' in text or text in texts:
            raise ValueError('duplicate_or_invalid_intervention')
        if 'primary' in arm and type(arm['primary']) is not bool:
            raise ValueError('invalid_primary_arm')
        arm_ids.add(arm_id)
        texts.add(text)
    if sum(arm.get('primary', False) for arm in arms) > 1:
        raise ValueError('multiple_primary_arms')
    if not isinstance(jobs, list) or not jobs:
        raise ValueError('jobs_required')
    baseline, original_ids = [], set()
    for job in jobs:
        if not isinstance(job, dict) or any(not isinstance(job.get(key), dict) for key in ('spec', 'variant', 'allocation')):
            raise ValueError('invalid_job')
        allocation, variant, spec = (job[key] for key in ('allocation', 'variant', 'spec'))
        run_id = allocation.get('run_id')
        if not isinstance(run_id, str) or not run_id or run_id in original_ids:
            raise ValueError('duplicate_or_invalid_original_id')
        original_ids.add(run_id)
        if variant.get('id') != 'B00L00':
            continue
        if (variant.get('control') != 'natural_original' or variant.get('factor') != 'B00'
                or type(variant.get('level')) is not int or variant['level'] != 0
                or variant.get('eligible') is not True or allocation.get('canonical_variant') != 'B00L00'):
            raise ValueError('invalid_natural_baseline')
        if (not isinstance(spec.get('task_id'), str) or not spec['task_id']
                or variant.get('task_id') != spec['task_id']
                or not isinstance(spec.get('request'), str) or variant.get('request') != spec['request']
                or any(variant.get(key) for key in ('history', 'files', 'env'))
                or variant.get('workspace', spec.get('workspace')) != spec.get('workspace')):
            raise ValueError('modified_natural_baseline')
        if type(allocation.get('seed')) is not int or allocation['seed'] <= 0:
            raise ValueError('invalid_original_seed')
        if type(allocation.get('continue_to_end')) is not bool:
            raise ValueError('invalid_original_full_flag')
        if 'system_prompt_intervention' in allocation:
            raise ValueError('original_already_has_intervention')
        baseline.append(job)
    if not baseline:
        raise ValueError('no_natural_baselines')
    baseline.sort(key=lambda job: job['allocation']['run_id'])
    source = {'jobs_object_sha256': digest(jobs), 'config_object_sha256': digest(config)}
    result = {'schema': 'causes.system_prompt_pairs.v1', 'namespace': namespace,
              'source': source, 'pairs': [], 'arms': []}
    for arm in arms:
        derived_config = copy.deepcopy(config)
        # The runner formats {workspace}; intervention braces must stay literal.
        literal_text = arm['text'].replace('{', '{{').replace('}', '}}')
        derived_config['system'] += '\n\n' + literal_text
        result['arms'].append({'arm': copy.deepcopy(arm), 'config': derived_config, 'jobs': []})
    generated_ids = set()
    for original in baseline:
        allocation = original['allocation']
        binding = {'original_run_id': allocation['run_id'], 'original_job_object_sha256': digest(original),
                   'original_config_object_sha256': source['config_object_sha256']}
        pair_id = namespace + '_p_' + digest(binding)[:24]
        pair = {'pair_id': pair_id, **binding, 'task_id': original['spec']['task_id'],
                'seed': allocation['seed'], 'continue_to_end': allocation['continue_to_end'], 'runs': {}}
        for output in result['arms']:
            arm = output['arm']
            run_id = pair_id + '_' + arm['id'] + '_' + digest(arm)[:12]
            if run_id in original_ids or run_id in generated_ids:
                raise ValueError('generated_identity_collision')
            generated_ids.add(run_id)
            job = copy.deepcopy(original)
            job['allocation'].update(
                run_id=run_id, stage='system_prompt_intervention', fixed_study_inference=False,
                primary_memberships=[],
                paired_baseline={**binding, 'original_allocation': copy.deepcopy(allocation)},
                system_prompt_intervention={'namespace': namespace, 'pair_id': pair_id,
                                            'arm': copy.deepcopy(arm), 'same_seed_intentional': True})
            output['jobs'].append(job)
            pair['runs'][arm['id']] = run_id
        result['pairs'].append(pair)
    return result


def _read_pin(reference, base):
    if not isinstance(reference, dict) or set(reference) != {'path', 'bytes', 'sha256'}:
        raise ValueError('invalid_input_pin')
    if not isinstance(reference['path'], str) or type(reference['bytes']) is not int:
        raise ValueError('invalid_input_pin')
    path = (base / reference['path']).resolve()
    data = path.read_bytes()
    if len(data) != reference['bytes'] or hashlib.sha256(data).hexdigest() != reference['sha256']:
        raise ValueError('input_pin_mismatch')
    return json.loads(data)


def _save(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    with path.open('xb') as stream:
        stream.write(data)
    return {'path': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def build_from_manifest(manifest_path, output_directory):
    """Verify frozen input files and create one exclusive offline output bundle."""
    manifest_path = Path(manifest_path).resolve()
    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    if not isinstance(manifest, dict) or manifest.get('schema') != 'causes.system_prompt_pair_input.v1':
        raise ValueError('invalid_input_manifest_schema')
    result = build_pairs(
        _read_pin(manifest['jobs'], manifest_path.parent),
        _read_pin(manifest['config'], manifest_path.parent), manifest['arms'],
        namespace=manifest['namespace'], expected_model=manifest['model'],
        expected_arm_count=manifest['expected_arm_count'])
    if type(manifest.get('expected_baseline_count')) is not int or len(result['pairs']) != manifest['expected_baseline_count']:
        raise ValueError('baseline_count_mismatch')
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=False)
    saved_input = _save(output / 'INPUT.json', manifest)
    arm_records = []
    for arm_output in result.pop('arms'):
        arm_id = arm_output['arm']['id']
        config_ref = _save(output / (arm_id + '.config.json'), arm_output['config'])
        jobs_ref = _save(output / (arm_id + '.jobs.json'), arm_output['jobs'])
        arm_records.append({'arm': arm_output['arm'], 'config': config_ref, 'jobs': jobs_ref})
    result.update(arms=arm_records, frozen_input=saved_input,
                  source_manifest={'path': str(manifest_path), 'bytes': len(original_manifest),
                                   'sha256': hashlib.sha256(original_manifest).hexdigest()})
    return _save(output / 'MANIFEST.json', result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_from_manifest(args.manifest, args.output)))


if __name__ == '__main__':
    main()
