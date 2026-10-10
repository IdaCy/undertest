import argparse
import datetime as dt
import json
from pathlib import Path

from causes import campaign_handoff as h
from causes import campaign_storage as s
from causes import campaign_storage_policy as policy_module


def release(plan_ref, authority_ref):
    """Release one owned barrier after accepted consumers and current policy checks."""
    plan = h.checked(plan_ref)
    s.require(plan['schema'] == 'causes.storage_consumer_qualified_release_plan.v1'
              and plan['source'] == h.pin(Path(__file__)), 'qualified_release_source_changed')
    policy = h.checked(plan['policy'])
    s.require(plan['role'] == policy['role'] and plan['role'] in ('mi', 'texas'),
              'qualified_release_role_changed')


    def consumers(value):
        accepted = h.checked(value['release_preparation_review'])
        s.require(value['release_plan'] == plan_ref
                  and value['root_adoption'] == plan['root_adoption']
                  and accepted['accepted_storage_release_preparation'] is True
                  and accepted['plan'] == plan_ref,
                  'qualified_release_authority_changed')
        h.checked_bytes(plan['root_adoption'])
        pair = plan['consumers']
        review = h.checked(pair['review'])
        evidence = h.checked(pair['evidence'])
        s.require(value['consumers'] == pair
                  and review['accepted_storage_phase_consumers'] is True
                  and review['evidence'] == pair['evidence']
                  and evidence['schema'] == 'causes.storage_phase_consumers.v1'
                  and evidence['accepted_storage_phase_consumers'] is True
                  and evidence['storage_registration'] == plan['phase']['evidence']
                  and evidence['phase_adoption'] == plan['phase_adoption'],
                  'qualified_release_consumers_not_accepted')
        pointer = h.checked(evidence['published_identity'])
        s.require(pointer['registration'] == plan['phase']['evidence']
                  and pointer['independent_review'] == plan['phase']['review']
                  and pointer['root_adoption'] == plan['phase_adoption'],
                  'qualified_release_consumer_phase_changed')
        start = dt.datetime.fromisoformat(evidence['observed_from'])
        end = dt.datetime.fromisoformat(evidence['observed_until'])
        current = dt.datetime.now(dt.timezone.utc)
        s.require(start.tzinfo is not None and end.tzinfo is not None
                  and start <= end <= current and (current - end).total_seconds() <= 180,
                  'qualified_release_consumers_not_fresh')
        for name in ('status_config', 'sync_config', 'status_result', 'sync_result'):
            h.checked_bytes(evidence[name])
        for name in ('status_execution', 'sync_execution'):
            execution = h.checked(evidence[name])
            s.require(execution['returncode'] == 0 and not execution.get('timeout', False),
                      'qualified_release_consumer_execution_failed')
        return True


    class QualifiedRelease(policy_module.StorageAdmission):
        def authority(self, action, *, capacity_owner=None):
            s.require(action == policy_module.RELEASE, 'only_qualified_release_permitted')
            ordinary = super().authority(action, capacity_owner=capacity_owner)
            def validate(value):
                ordinary.validate_policy(value)
                return consumers(value)
            return h.Authority(ordinary.reference, action, ordinary.pause_paths, validate)


    def waiting_owner_process():
        registration = policy_module._accepted(
            plan['phase'], 'accepted_storage_phase_registration', 'registration')
        role = 'new_mi' if plan['role'] == 'mi' else 'new_texas'
        expected = ('mi_waiting',) if plan['role'] == 'mi' else (
            'texas_waiting', 'texas_waiting_mi_retired')
        owner = registration['owners'][role]
        s.require(registration['phase'] in expected
                  and owner['observation_state'] == 'waiting',
                  'qualified_release_requires_accepted_waiting_owner')
        return owner['process']


    operator = QualifiedRelease(plan['policy'], plan['readiness'], authority_ref)
    operator.authority(policy_module.RELEASE, capacity_owner=waiting_owner_process()).check()
    directory = h.new_record_directory(plan['receipt_directory'])
    try:
        h.save_new(directory, 'INTENT.json', {'at': h.now(), 'plan': plan_ref,
            'authority': authority_ref, 'consumers': plan['consumers'], 'phase': plan['phase']})
        released = operator.release(plan['phase'], execute=True)
        result = h.save_new(directory, 'RESULT.json', {'at': h.now(), 'plan': plan_ref,
            'release': released, 'passed': True, 'actual_running_observation_pending': True,
            'automatic_retry': False})
        return {'passed': True, 'result': result}
    except BaseException as error:
        h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error': repr(error),
            'barrier_may_have_been_released': True, 'manual_reconciliation_required': True,
            'automatic_retry': False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--authority', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(release(json.loads(args.plan), json.loads(args.authority))))


if __name__ == '__main__':
    main()
