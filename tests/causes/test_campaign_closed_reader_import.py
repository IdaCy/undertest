import copy

import unittest

from causes.campaign_closed_reader_import import select_closed_rows


def fixture():
    ready = [{'measurement_key': key, 'run_id': 'run_' + key, 'stage': 'early'}
             for key in ('old', 'new', 'unresolved')]
    preparation = {'counts': {'reader_ready': 3, 'copied': 4, 'unavailable': 1,
                              'selected': 10, 'pending': 6,
                              'reader_requests': 9, 'self_requests': 0}}
    plan = {'previous_cohort_claimed_keys': ['old'], 'expected_unclaimed_contexts': 2}
    closed = {'claimed': 2, 'ready': 2, 'finished_or_unresolved': 2,
              'assigned': 4, 'active': 0, 'unclaimed': 0, 'unclaimed_keys': [],
              'all_claims_accounted': True, 'claimed_keys': ['new', 'unresolved'],
              'owned_pause': None, 'stop_reason': None, 'quarantine': None}
    events = [{'event': 'claimed', 'key': 'new'},
              {'event': 'claimed', 'key': 'unresolved'},
              {'event': 'unresolved_current_reader_attempt', 'key': 'unresolved'},
              {'event': 'finished', 'key': 'new'},
              {'event': 'finite_selection_complete', 'new_admissions_stopped': True,
               'reason': None}]
    return preparation, ready, plan, closed, events


def test_selects_only_new_attempts_and_retains_unresolved_terminal():
    by, terminals = select_closed_rows(*fixture(), 'additive')
    assert list(by) == ['new', 'unresolved']
    assert [event['key'] for event in terminals] == ['unresolved', 'new']
    assert terminals[0]['event'] == 'unresolved_current_reader_attempt'


def test_fixed_batch_requires_and_imports_every_prepared_row():
    prep, ready, plan, closed, events = fixture()
    ready = ready[1:]
    prep['counts'] = {'reader_ready': 2, 'selected': 2, 'context_ready': 2,
                      'unavailable': 0}
    closed['assigned'] = 2
    by, _ = select_closed_rows(prep, ready, {}, closed, events, 'fixed')
    assert set(by) == {'new', 'unresolved'}


def test_refuses_changed_membership_attempts_or_closure(case):
    prep, ready, plan, closed, events = fixture()
    if case == 'replay':
        plan['previous_cohort_claimed_keys'].append('new')
    elif case == 'omitted_prepared':
        ready.append({'measurement_key': 'forgotten', 'run_id': 'run_forgotten',
                      'stage': 'early'})
        prep['counts'].update(reader_ready=4, copied=5, reader_requests=12, pending=5)
        closed['assigned'] = 5
    elif case == 'duplicate_run':
        ready[1]['run_id'] = ready[0]['run_id']
    elif case == 'duplicate_terminal':
        events.insert(-1, copy.deepcopy(events[-2]))
    elif case == 'unaccounted_claim':
        events.pop(2)
    elif case == 'active':
        closed['active'] = 1
    elif case == 'quarantine':
        closed['quarantine'] = {'reason': 'retained failure'}
    elif case == 'non_natural_end':
        events[-1]['reason'] = 'administrative hold'
    elif case == 'planned_count':
        plan['expected_unclaimed_contexts'] = 3
    elif case == 'invalid_count':
        prep['counts']['pending'] = -1
    elif case == 'stage':
        ready[1]['stage'] = 'end'
    elif case == 'unknown_prior':
        plan['previous_cohort_claimed_keys'] = ['unknown']
    with unittest.TestCase().assertRaises(ValueError):
        select_closed_rows(prep, ready, plan, closed, events, 'additive')


def test_keeps_inputs_unchanged():
    values = fixture()
    before = copy.deepcopy(values)
    select_closed_rows(*values, 'additive')
    assert values == before


class ClosedReaderImportTests(unittest.TestCase):
    pass


for _name in ('test_selects_only_new_attempts_and_retains_unresolved_terminal',
              'test_fixed_batch_requires_and_imports_every_prepared_row',
              'test_keeps_inputs_unchanged'):
    setattr(ClosedReaderImportTests, _name, lambda self, fn=globals()[_name]: fn())
for _case in ('replay', 'omitted_prepared', 'duplicate_run', 'duplicate_terminal',
              'unaccounted_claim', 'active', 'quarantine', 'non_natural_end',
              'planned_count', 'invalid_count', 'stage', 'unknown_prior'):
    setattr(ClosedReaderImportTests, 'test_refuses_' + _case,
            lambda self, case=_case: test_refuses_changed_membership_attempts_or_closure(case))


if __name__ == '__main__':
    unittest.main()
