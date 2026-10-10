import copy
import datetime as dt
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from causes import campaign_capacity_trial as trial
from causes import campaign_capacity_summary as summary
from causes import campaign_handoff as h
from test_campaign_capacity_wait import waiting_fixture
from test_campaign_capacity_summary import fixture, journal, wall, START, accepted_reference, criteria


def replace(value, old, new):
    if value == old:
        return copy.deepcopy(new)
    if isinstance(value, dict):
        return {key: replace(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item, old, new) for item in value]
    return value


class EndpointIntegrationTest(unittest.TestCase):
    def test_real_prelude_actor_budget_revokes_health_and_holds_before_confirmation(self):
        _, _, boundary, _, declaration = fixture()
        declaration['owner']['max_full'] = 4
        events = boundary['journals'][0]['events']
        for index in range(4):
            episode = next(row for row in boundary['requests']['episodes'] if row['run_id'] == str(index))
            episode['full'] = episode['claim']['full'] = True
            terminal = {'event': 'episode_finished', 'controller_id': 'current',
                        'run_id': str(index), 'at': wall(index + 1)}
            episode['final'] = terminal
            episode['terminal_metadata'] = {'classification': 'technical' if index < 3 else 'natural'}
            events.append(terminal)
        boundary['journals'] = [journal(events)]
        result = summary.evaluate_prelude(boundary, declaration, accepted_reference(), criteria(),
                                         cohort_start=START.timestamp() - 20, end_wall=wall(5))
        self.assertEqual(result['decision'], 'hold')
        self.assertEqual(result['actor_guard']['technical'], 3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = {'pause_file': str(root / 'MI_PAUSE'), 'output_root': str(root / 'output'), 'trial_id': 'fixture'}
            writer = Mock()
            supervisor = trial.CandidateSupervisor(writer, Mock(), binding, root)
            supervisor.last_sample = h.save_new(root, 'SAMPLE.json', {
                'finished_at': h.now(), 'finished_monotonic_seconds': time.monotonic()})
            supervisor.begin_endpoint(0)
            supervisor.finish_endpoint(0, result)
            self.assertTrue(supervisor.held)
            self.assertTrue(Path(binding['pause_file']).is_file())
            writer.close.assert_called_once()
            self.assertEqual([call.args[0] for call in writer.publish.call_args_list], ['endpoint_pending', 'held'])

    def test_real_metadata_collector_and_exact_pending_proof_preserve_raw_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples, start, end, window, declaration, evidence, event = waiting_fixture()
            declaration['campaign_root'] = str(root)
            declaration['model_key'] = 'model'
            declaration['guard']['directory'] = str(root / 'storage')
            Path(declaration['guard']['directory']).mkdir()
            (root / 'leases').mkdir()
            capacity = declaration['guard']['binding']['capacity_guard']
            capacity['lease_path'] = str(root / 'leases/CURRENT.json')
            capacity['plan_path'] = str(root / 'PLAN.json')
            lease = evidence['leases'][0]
            lease['value']['binding'] = copy.deepcopy(capacity)
            lease['value']['plan']['path'] = capacity['plan_path']
            new_lease = h.save_new(root / 'leases', 'lease_00000000.json', lease['value'])
            samples, evidence = replace([samples, evidence], lease['pin'], new_lease)
            transition = evidence['transitions'][0]
            transition['value']['binding_sha256'] = trial.hashlib.sha256(
                h.encoded(declaration['guard']['binding'])).hexdigest()
            new_transition = h.save_new(root / 'storage', '00000000.json', transition['value'])
            samples, evidence = replace([samples, evidence], transition['pin'], new_transition)
            for log in end['journals']:
                log['path'] = str(root / 'runs/fixed/model/.dispatcher/journal.jsonl')
            times = iter([end['capture']['started']['wall'], end['capture']['finished']['wall'],
                          end['capture']['started']['wall'], end['capture']['finished']['wall']])
            monos = iter([2800.2, 2800.3, 2800.2, 2800.3])
            original_samples = copy.deepcopy(samples)
            with patch.object(trial.h, 'now', side_effect=lambda: next(times)), \
                 patch.object(trial.time, 'monotonic', side_effect=lambda: next(monos)):
                captured = trial.collect_endpoint(root / 'boundary', end['journals'],
                    dt.datetime.fromisoformat(window['start_wall']).timestamp() - 30,
                    declaration, samples)
            actual = summary.summarize_window(samples, start, captured, window, declaration,
                capacity_wait_evidence=captured['capacity_wait_evidence'])
            self.assertTrue(actual['support']['supported'], actual['support'])
            self.assertEqual(actual['capacity_wait_support']['sample_indices'], [1])
            self.assertEqual(len(actual['capacity_wait_support']['event_sha256']), 1)
            self.assertEqual(samples, original_samples)
            self.assertFalse(samples[1]['storage_guard']['healthy'])
            for name in ('requests.json', 'journals.json', 'capture.json', 'capacity_wait_evidence.json'):
                self.assertTrue((root / 'boundary' / name).is_file())

    def test_sample_only_pending_lease_is_retained_without_floor_wait_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'storage').mkdir()
            lease = h.save_new(root, 'lease.json', {'fixture': 'sample only'})
            declaration = {'guard': {'directory': str(root / 'storage')}}
            samples = [{'storage_guard': {'capacity_guard': {'lease': lease}}}]
            with patch.object(trial, 'capture_candidate_boundary', return_value={}):
                result = trial.collect_endpoint(root, [], 0, declaration, samples)
            self.assertEqual(result['capacity_wait_evidence']['transitions'], [])
            self.assertEqual(result['capacity_wait_evidence']['leases'],
                             [{'pin': lease, 'value': {'fixture': 'sample only'}}])


if __name__ == '__main__':
    unittest.main()
