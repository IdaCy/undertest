from causes import campaign_reader as reader
from causes import neutral_summary_view as paired


def build_neutral_arm(episode, summary_row, construction, *, arm, baseline_config):
    """Derive one observed arm without inventing its missing counterpart.

    The caller pins the construction, summary, original episode and baseline
    configuration. Provenance stays separate from the returned model view.
    """
    require = paired._require
    require(arm in ('baseline', 'treatment'), 'neutral_arm_invalid')
    require(isinstance(construction, dict) and set(construction) == {
        'task_id', 'baseline_variant', 'condition_variant', 'baseline_variant_sha256',
        'condition_variant_sha256', 'repository', 'baseline_workspace'},
        'neutral_construction_fields_invalid')
    baseline = paired._variant(construction['baseline_variant'])
    condition = paired._variant(construction['condition_variant'])
    require(reader.digest(baseline) == construction['baseline_variant_sha256']
            and reader.digest(condition) == construction['condition_variant_sha256'],
            'neutral_construction_pin_mismatch')
    task_id = construction['task_id']
    repository, workspace = construction['repository'], construction['baseline_workspace']
    require(isinstance(task_id, str) and task_id
            and baseline['task_id'] == condition['task_id'] == task_id
            and isinstance(repository, str) and len(repository.split('/')) == 2
            and all(repository.split('/')) and isinstance(workspace, str) and workspace.startswith('/'),
            'neutral_task_identity_mismatch')
    require(baseline['id'] == 'B00L00' and baseline.get('control') == 'natural_original'
            and not any(key in baseline for key in ('history', 'workspace', 'files', 'env')),
            'neutral_unchanged_baseline_required')
    paired._expected_condition(baseline, condition, repository, workspace)
    require(isinstance(summary_row, dict) and summary_row.get('task_id') == task_id
            and summary_row.get('baseline_request_sha256') == paired._text_hash(baseline['request'])
            and isinstance(summary_row.get('summary'), str) and summary_row['summary'].strip()
            and '\0' not in summary_row['summary']
            and summary_row.get('summary_sha256') == paired._text_hash(summary_row['summary']),
            'neutral_summary_provenance_mismatch')
    require(isinstance(baseline_config, dict) and isinstance(baseline_config.get('system'), str),
            'neutral_baseline_system_required')
    variant = baseline if arm == 'baseline' else condition
    paired._episode(episode, variant, task_id, baseline['request'], repository, workspace)
    require(episode['config']['system'] == baseline_config['system'],
            'neutral_single_arm_system_mismatch')
    system = baseline_config['system'].format(workspace=workspace)
    view, proof = paired._derive(episode, summary_row['summary'], system)
    return view, {
        'schema': 'causes.neutral_summary_single_arm_derivation.v1',
        'arm': arm,
        'construction_sha256': reader.digest(construction),
        'summary_row_sha256': reader.digest(summary_row),
        'summary_sha256': summary_row['summary_sha256'],
        'baseline_config_sha256': reader.digest(baseline_config),
        'original_episode_sha256': reader.digest(episode),
        'derivation': proof,
        'counterpart_context_supplied': False,
        'initial_context_only': True,
        'external_tool_neutralization_supported': False,
        'residual_external_cues_may_remain': True}
