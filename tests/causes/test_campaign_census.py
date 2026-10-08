import copy
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_census as census


def allocation(rid, seed=1):
    return dict(run_id=rid, seed=seed, block_seed=2, task_seed=None, model_seed=3)


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.hosts = []
        for name in ('one', 'two'):
            base = self.base/name
            (base/'runs/finite/model/.dispatcher/blocks').mkdir(parents=True)
            self.hosts.append(dict(host=name, base=str(base), roots=['runs/finite/model'],
                                  top_jobs=[], control_directories=[], limits=dict(
                                      max_file_bytes=10000, max_total_bytes=100000,
                                      max_files=20, max_directory_entries=20, max_allocations=30)))
        self.config = dict(schema=census.CONFIG_SCHEMA, collector_sha256='a'*64,
                           hosts=self.hosts, frozen_inputs=[])

    def write(self, host, relative, value):
        path = Path(self.hosts[host]['base'])/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(census.encoded(value))
        return path

    def captures(self):
        out = {}
        for host in self.hosts:
            value = census.collect_host(host)
            value['collector_source'] = dict(path='/public/campaign_census.py', bytes=100, sha256='a'*64)
            raw = census.encoded(value)
            out[host['host']] = {'value': value, 'pin': census.pin(self.base/(host['host']+'.json'), raw)}
        return out

    def freeze(self, rows, locations=None):
        raw = census.encoded(rows);pin = census.pin(self.base/'frozen.json', raw)
        spec = dict(pin=pin, selector=[], kind='allocations', expected_count=len(rows))
        if locations is not None:
            spec['allowed_locations'] = locations
        self.config['frozen_inputs'] = [spec]
        return [dict(pin=pin, value=rows)]

    def test_both_hosts_and_full_unissued_blocks_are_reserved(self):
        self.write(0, 'runs/finite/model/issued/allocation.json', allocation('issued'))
        self.write(0, 'runs/finite/model/.dispatcher/blocks/0001.json',
                   {'jobs': [{'allocation': allocation('issued')}, {'allocation': allocation('unissued', 91)}]})
        self.write(1, 'runs/finite/model/other/allocation.json', allocation('other', 75))
        frozen = self.freeze([allocation('future', 103)])
        result = census.compose_reservation(self.config, self.captures(), frozen)
        self.assertEqual(result['run_ids'], ['future', 'issued', 'other', 'unissued'])
        self.assertEqual(result['allocation_seeds'], [1, 2, 3, 75, 91, 103])
        self.assertEqual(result['persisted_full_blocks'][0]['allocation_count'], 2)
        self.assertFalse(result['root_admission_authorized'])
        self.assertEqual(len(result['host_cuts']), 2)

    def test_repeated_exact_identity_allowed_but_drift_refused(self):
        self.write(0, 'runs/finite/model/same/allocation.json', allocation('same'))
        self.write(1, 'runs/finite/model/same/allocation.json', allocation('same'))
        self.assertEqual(census.compose_reservation(self.config, self.captures(), [])['run_ids'], ['same'])
        self.write(1, 'runs/finite/model/same/allocation.json', allocation('same', 9))
        with self.assertRaisesRegex(ValueError, 'identity_conflict'):
            census.compose_reservation(self.config, self.captures(), [])

    def test_typed_seed_and_strict_json_refusals(self):
        for bad in (True, 1.0, '1', -1, 2**31-1):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'typed_allocation'):
                census.identity(allocation('same', bad))
        for raw in (b'{"seed":1,"seed":2}', b'{"seed":NaN}', b'{"seed":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                census.strict_json(raw)
        for bad in ('', None, True, '../x', 'a/b', '.'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                census.identity(allocation(bad))

    def test_exact_root_and_top_job_inventories(self):
        (Path(self.hosts[0]['base'])/'runs/unregistered/model').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'unreviewed_run_root_inventory'):
            census.collect_host(self.hosts[0])
        self.write(1, 'forgotten_jobs.json', [])
        with self.assertRaisesRegex(ValueError, 'pilot_job_inventory'):
            census.collect_host(self.hosts[1])

    def test_basename_and_static_host_root_binding(self):
        self.write(0, 'runs/finite/model/wrong/allocation.json', allocation('different'))
        with self.assertRaisesRegex(ValueError, 'basename'):
            census.collect_host(self.hosts[0])
        self.write(0, 'runs/finite/model/wrong/allocation.json', allocation('wrong'))
        frozen = self.freeze([allocation('wrong')], [['two', 'runs/finite/model']])
        with self.assertRaisesRegex(ValueError, 'wrong_host_or_root'):
            census.compose_reservation(self.config, self.captures(), frozen)

    def test_missing_attempt_is_preserved_then_refused_even_if_reserved(self):
        (Path(self.hosts[0]['base'])/'runs/finite/model/partial').mkdir()
        frozen = self.freeze([allocation('partial')])
        captures = self.captures()
        self.assertEqual(captures['one']['value']['attempt_directories_without_allocation'][0]['run_id'], 'partial')
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            census.compose_reservation(self.config, captures, frozen)

    def test_symlink_and_read_bounds(self):
        target = self.write(0, 'outside.json', allocation('link'))
        folder = Path(self.hosts[0]['base'])/'runs/finite/model/link';folder.mkdir()
        (folder/'allocation.json').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            census.collect_host(self.hosts[0])
        self.write(1, 'runs/finite/model/large/allocation.json', allocation('large'))
        self.hosts[1]['limits']['max_file_bytes'] = 8
        with self.assertRaisesRegex(ValueError, 'size'):
            census.collect_host(self.hosts[1])

    def test_control_exception_exact_and_metadata_only(self):
        control = Path(self.hosts[0]['base'])/'runs/finite/model/.launch';control.mkdir()
        (control/'stdout').write_text('not inspected')
        self.hosts[0]['control_directories'] = ['runs/finite/model/.launch']
        self.write(0, 'runs/finite/model/issued/allocation.json', allocation('issued'))
        payload = self.write(0, 'runs/finite/model/issued/summary.json', {'private': 'not inspected'})
        original = census._bounded_read
        def guard(path, limit):
            self.assertNotEqual(Path(path), payload)
            self.assertEqual(Path(path).name, 'allocation.json')
            return original(path, limit)
        with patch.object(census, '_bounded_read', side_effect=guard):
            captures = self.captures()
        self.assertEqual(census.compose_reservation(self.config, captures, [])['run_ids'], ['issued'])
        captures['one']['value']['control_directories'] = []
        with self.assertRaisesRegex(ValueError, 'control_exception'):
            census.compose_reservation(self.config, captures, [])

    def test_capture_tampering_and_missing_host_refused(self):
        self.write(0, 'runs/finite/model/issued/allocation.json', allocation('issued'))
        captures = self.captures()
        with self.assertRaisesRegex(ValueError, 'exact_census_inputs'):
            census.compose_reservation(self.config, {'one': captures['one']}, [])
        for field, bad in [('host', 'wrong'), ('host_config_sha256', 'b'*64), ('reads_actor_or_probe_outcomes', True)]:
            altered = copy.deepcopy(captures);altered['one']['value'][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                census.compose_reservation(self.config, altered, [])
        altered = copy.deepcopy(captures)
        altered['one']['value']['files'][0]['path'] = '/elsewhere/issued/allocation.json'
        with self.assertRaisesRegex(ValueError, 'path_scope'):
            census.compose_reservation(self.config, altered, [])

    def test_cli_collect_and_compose_only_explicit_local_files(self):
        self.write(0, 'runs/finite/model/issued/allocation.json', allocation('issued'))
        source = Path(census.__file__).resolve()
        self.config['collector_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.config.update(max_capture_bytes=100000, max_frozen_bytes=100000)
        config_path = self.base/'config.json';config_path.write_bytes(census.encoded(self.config))
        records = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for host in self.hosts:
                output = self.base/(host['host']+'-capture.json')
                census.main(['collect', '--config', str(config_path), '--host', host['host'], '--output', str(output)])
                records[host['host']] = census.pin(output, output.read_bytes())
            captures = self.base/'captures.json';captures.write_bytes(census.encoded(records))
            output = self.base/'reservation.json'
            census.main(['compose', '--config', str(config_path), '--captures', str(captures), '--output', str(output)])
        result = census.strict_json(output.read_bytes())
        self.assertEqual(result['run_ids'], ['issued'])
        self.assertEqual(result['capture_map'], census.pin(captures, captures.read_bytes()))
        self.assertEqual(result['configuration'], census.pin(config_path, config_path.read_bytes()))

    def test_total_and_directory_bounds_are_enforced(self):
        for rid in ('one', 'two'):
            self.write(0, f'runs/finite/model/{rid}/allocation.json', allocation(rid))
        self.hosts[0]['limits']['max_total_bytes'] = 100
        with self.assertRaisesRegex(ValueError, 'size'):
            census.collect_host(self.hosts[0])
        self.hosts[0]['limits']['max_total_bytes'] = 10000
        self.hosts[0]['limits']['max_directory_entries'] = 2
        with self.assertRaisesRegex(ValueError, 'directory_entry_bound'):
            census.collect_host(self.hosts[0])

    def test_exclusive_durable_output_and_pinned_read(self):
        path = self.base/'new.json'
        record = census.save_exclusive(path, {'count': 1})
        self.assertEqual(census._load_pin(record, 100), {'count': 1})
        with self.assertRaises(FileExistsError):
            census.save_exclusive(path, {'count': 2})
        path.write_text('{"count":2}\n')
        with self.assertRaisesRegex(ValueError, 'pinned_input_changed'):
            census._load_pin(record, 100)


if __name__ == '__main__':
    unittest.main()
