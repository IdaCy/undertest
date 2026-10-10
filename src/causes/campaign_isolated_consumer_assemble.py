"""Join one retained status/JP1 pair to an accepted native waiting foundation.

Local metadata only. No observer, admission, source publication or body reads.
The output requires independent actual acceptance before native release.
"""
import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
from causes.campaign_consumer_ownership import pin, checked, save



def check_native(plan, status, records, reader=checked, mapped=None, ownership=None):
    native = plan['native']
    policy = reader(native['policy'])
    foundation = reader(native['foundation']['evidence'])
    review = reader(native['foundation']['review'])
    assert review.get('accepted_native_admission_foundation') is True
    assert review.get('foundation') == native['foundation']['evidence']
    assert foundation['policy'] == native['policy'] and foundation['stage'] == 'waiting_release'
    assert native['waiting_report'] in foundation['evidence_pins']
    report = reader(native['waiting_report'])
    assert report['host'] == policy['controller_host'] and report['stage'] == 'waiting_release'
    assert report['action'] == 'foundation' and report['read_only'] is True
    waiting = report['observation']['waiting']
    assert report['observation']['waiting_launch'] == native['launch']
    _, raw = mapped(report['receipt_map'], native['launch'])
    launch = json.loads(raw)
    assert waiting['policy'] == native['policy'] and waiting['launch'] == native['launch']
    assert launch['policy'] == native['policy'] and launch['output_root'] == policy['output_root']
    assert waiting['process'] == launch['process'] and waiting['owned_barrier'] == launch['owned_barrier']
    assert waiting['zero_initial_claims'] is True and waiting['zero_persisted_blocks'] is True
    assert waiting['previous_owners_retired'] is True and waiting['controls_clear'] is True
    assert foundation['legacy_owners_healthy'] is True
    registration = reader(native['registration'])
    registration_review = reader(native['registration_review'])
    pointer = reader(native['authorization_pointer'])
    monitor = reader(native['monitor_policy'])
    assert registration_review.get('accepted_companion_registration') is True
    assert registration_review.get('registration') == native['registration']
    assert pointer['registration'] == native['registration']
    assert pointer['policy'] == native['monitor_policy']
    assert registration['controller']['process'] == launch['process']
    assert registration['controller']['launch_receipt'] == native['launch']
    assert registration['donor_histories'] == policy['donor_histories']
    assert monitor['sync_roots'] == policy['required_sync_roots']
    assert status['companion']['valid'] is True
    assert ownership['live_exclusive_owner_verified'] is True
    assert ownership['historical_stop_used'] is False
    times = [dt.datetime.fromisoformat(row['ended_at']) for row in records.values()]
    end = max(times)
    assert dt.datetime.fromisoformat(waiting['at']) <= end
    return dict(at=end.isoformat(), policy=native['policy'], launch=native['launch'],
        owned_barrier=launch['owned_barrier'], zero_initial_claims=True, zero_persisted_blocks=True,
        legacy_owners_healthy=True, mandatory_companion_valid=True, snapshot_ownership_valid=True,
        required_sync_roots=policy['required_sync_roots'], registration=native['registration'],
        authorization_pointer=native['authorization_pointer'], monitor_policy=native['monitor_policy'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--review', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--foundation-binding', required=True)
    parser.add_argument('--foundation-binding-review', required=True)
    args = parser.parse_args()
    plan_pin, review_pin = pin(Path(args.plan)), pin(Path(args.review))
    plan, review = checked(plan_pin), checked(review_pin)
    assert review['accepted_storage_phase_consumer_execution'] is True and review['plan'] == plan_pin
    assert plan['assembly_source'] == pin(Path(__file__).resolve())
    join_source = plan['join_source']
    assert pin(join_source['path']) == join_source
    join_review = checked(plan['join_source_review'])
    assert join_review.get('accepted_native_consumer_join_sources') is True
    assert join_review['source'] == join_source
    join_spec = importlib.util.spec_from_file_location('native_consumer_join', join_source['path'])
    joiner = importlib.util.module_from_spec(join_spec)
    join_spec.loader.exec_module(joiner)
    binding_pin = pin(Path(args.foundation_binding))
    binding_review_pin = pin(Path(args.foundation_binding_review))
    plan = joiner.join_native_foundation(plan_pin, review_pin, binding_pin, binding_review_pin, checked)
    ownership_source = plan['ownership_source']
    assert pin(ownership_source['path']) == ownership_source
    spec = importlib.util.spec_from_file_location('isolated_consumer_ownership', ownership_source['path'])
    owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
    config = checked(plan['observation_configuration'])
    owner.configuration_layout(config, plan['ownership']['layout'])
    execution_dir = Path(plan['execution_directory'])
    assert str(execution_dir) == plan['ownership']['layout']['execution_directory']
    records = {}
    for action in ('status', 'sync'):
        entry_config = checked(plan['entries'][action]['configuration'])
        assert entry_config['action'] == action and entry_config['observation_config'] == plan['observation_configuration']
        receipt = checked(pin(execution_dir / action / 'EXECUTION.v1.json'))
        assert receipt['plan'] == plan_pin and receipt['review'] == review_pin and receipt['action'] == action
        assert receipt['returncode'] == 0 and receipt['error'] is None
        assert receipt['source_unchanged'] is True and receipt['configuration_unchanged'] is True
        assert receipt['source'] == plan['entries'][action]['source']
        assert receipt['configuration'] == plan['entries'][action]['configuration']
        for field in ('source', 'configuration', 'stdout', 'stderr'):
            assert pin(receipt[field]['path']) == receipt[field]
        assert receipt['stderr']['bytes'] == 0
        assert dt.datetime.fromisoformat(receipt['started_at']) <= dt.datetime.fromisoformat(receipt['ended_at'])
        records[action] = receipt
    lines = Path(records['status']['stdout']['path']).read_text().splitlines()
    assert len(lines) == 2
    status_path = Path(lines[0])
    assert status_path.parent == Path(plan['ownership']['layout']['status_directory'])
    status = checked(pin(status_path))
    assert status == json.loads(lines[1])
    assert status['source_identity']['all_expected_identities_match'] is True
    overlay = status['source_identity']['overlay']
    assert overlay['authorized'] is True and overlay['phase'] == plan['storage_phase']
    assert overlay['receipt'] == plan['storage_registration']
    assert overlay['manifest_sha256'] == plan['identity_manifest']['sha256']
    assert status['companion']['valid'] is True
    assert len(status['alarms']) == 6 and all(type(x) is bool for x in status['alarms'].values())
    assert all(not value for key, value in status['alarms'].items() if key != 'root_free_below_80GiB')
    sync = checked(records['sync']['stdout'])
    assert sync['skipped'] == []
    attempt = Path(sync['attempt'])
    assert attempt.parent == Path(plan['ownership']['layout']['snapshot_destination']) / 'attempts'
    verified = checked(pin(attempt / 'VERIFIED.json'))
    assert len(verified['runs']) == sync['verified_runs']
    assert sum(row['bytes'] for row in verified['runs']) == sync['verified_bytes']
    manifest_pin = pin(attempt / 'MANIFEST.json')
    assert all(row['manifest_sha256'] == manifest_pin['sha256'] for row in verified['runs'])
    request_pin = pin(attempt / 'REQUEST.json')
    authorization_pin = pin(attempt / 'AUTHORIZATION.json')
    assert checked(request_pin)['accepted'] == []
    policy = checked(plan['native']['policy'])
    native = plan['native']
    campaign_root = str(Path(plan['campaign_root']))
    assert config['snapshot']['remote_base'] == config['legacy']['remote_base'] == policy['base'] == campaign_root
    assert policy['output_root'].startswith(campaign_root + '/runs/')
    joiner.verify_snapshot_result(sync, checked(request_pin), checked(authorization_pin),
        checked(manifest_pin), verified, manifest_pin, host=policy['controller_host'],
        remote_base=campaign_root, roots=policy['required_sync_roots'],
        authorization_evidence=dict(authorization=native['authorization_pointer'],
            policy=native['monitor_policy'], registration=native['registration'],
            review=native['registration_review'], legacy_identity_receipt=plan['storage_registration']))
    source = plan['native']['foundation_source']
    assert pin(source['path']) == source
    receipt_refs = {action: pin(execution_dir / action / 'EXECUTION.v1.json') for action in ('status', 'sync')}
    ownership_ref = pin(Path(plan['ownership']['layout']['workspace']) / 'OWNERSHIP_ACTUAL.json')
    ownership = owner.verify_live_ownership(ownership_ref, plan_pin, plan['ownership'], receipt_refs)
    storage = owner.verify_snapshot_storage(sync, checked(manifest_pin), verified, plan['ownership']['layout'])
    readiness = check_native(plan, status, records, mapped=owner.mapped, ownership=ownership)
    workflow = checked(ownership['adoption'])
    assert workflow['root_authorized'] is True and workflow['workflow'] == plan['workflow']
    evidence_pins = [plan_pin, review_pin, pin(status_path), records['sync']['stdout'],
        pin(attempt / 'VERIFIED.json'), pin(attempt / 'AUTHORIZATION.json'), manifest_pin,
        *(pin(execution_dir / action / 'EXECUTION.v1.json') for action in ('status', 'sync')),
        plan['native']['foundation']['evidence'], plan['native']['foundation']['review'],
        plan['native']['waiting_report'], ownership_ref, plan['ownership_source'], plan['observation_configuration'], storage['index'],
        plan['native']['registration'], plan['native']['registration_review'],
        plan['native']['authorization_pointer'], plan['native']['monitor_policy'],
        request_pin, binding_pin, binding_review_pin, join_source, plan['join_source_review']]
    readiness.update(schema='causes.native_startup_consumers.v1',
        assembled_at=dt.datetime.now(dt.timezone.utc).isoformat(), evidence_pins=evidence_pins,
        execution_plan=plan_pin, execution_plan_review=review_pin,
        foundation_binding=binding_pin, foundation_binding_review=binding_review_pin,
        join_source=join_source, join_source_review=plan['join_source_review'],
        observed_from=min(row['started_at'] for row in records.values()),
        observed_until=max(row['ended_at'] for row in records.values()),
        original_intervals=records, status_result=pin(status_path),
        sync_verified=pin(attempt / 'VERIFIED.json'), sync_manifest=manifest_pin,
        sync_authorization=pin(attempt / 'AUTHORIZATION.json'), status_alarms=status['alarms'],
        isolated_ownership=ownership_ref, snapshot_storage=storage,
        actor_admission=False, independent_actual_review_required=True,
        workflow_adoption=ownership['adoption'], assembly_source=pin(Path(__file__).resolve()))
    output = Path(args.output)
    assert output.is_absolute() and output.resolve() == output and output.is_relative_to(Path(plan['campaign_root']) / 'tasks')
    print(json.dumps(save(output, readiness)))


if __name__ == '__main__':
    main()
