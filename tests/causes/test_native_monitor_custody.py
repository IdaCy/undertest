import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from causes import campaign_handoff as h, campaign_monitor as m
from causes import campaign_corpus_succession as s, corpus_allocation, corpus_continuation
from tests.causes import test_native_corpus_succession as fixtures


class NativeMonitorCustodyTests(unittest.TestCase):
    setUp = fixtures.ClosedIssuedTests.setUp
    save = fixtures.ClosedIssuedTests.save
    allocation = fixtures.NativeAllocationTests.allocation
    runtime = fixtures.NativeAllocationTests.runtime
    lineage = fixtures.NativeMonitorTests.lineage

    def case(self, custody=True):
        policy, registration, objects, _ = self.lineage()
        metadata = self.root / 'metadata'
        metadata.mkdir()
        resolved = {}

        def normalize(value):
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if not isinstance(value, dict):
                return value
            if set(value) == {'path', 'bytes', 'sha256'} and value['path'] in objects:
                name = value['path']
                if name not in resolved:
                    contents = normalize(objects[name])
                    target = metadata / (str(len(resolved)) + '_' + Path(name).name)
                    target.write_bytes(m.encoded(contents))
                    resolved[name] = h.pin(target)
                return resolved[name]
            return {key: normalize(item) for key, item in value.items()}

        policy, registration = normalize(policy), normalize(registration)
        policy['native_successions']['validator_sources'] = {
            'campaign_corpus_succession': h.pin(s.__file__),
            'corpus_allocation': h.pin(corpus_allocation.__file__),
            'corpus_continuation': h.pin(corpus_continuation.__file__),
        }
        old_module = self.root / 'registered_monitor.py'
        old_module.write_bytes(b'# independently accepted registered monitor\n')
        local_module = h.pin(m.__file__)
        registration['companion_source_sha256'] = h.pin(old_module)['sha256'] if custody else local_module['sha256']
        policy_ref = self.save('monitor-policy.json', policy)
        registration_ref = self.save('registration.json', registration)
        accepted = self.save('registration-review.json', dict(
            accepted_companion_registration=True, registration=registration_ref))
        auth = dict(schema='caus.jp_companion_authorization.v1', policy=policy_ref,
                    registration=registration_ref, independent_review=accepted,
                    companion_module=local_module)
        row = policy['native_successions']['links'][0]['predecessor']
        closure_ref = row['closure']['evidence']
        closure = h.checked(closure_ref)
        originals = [closure_ref, closure['never_claimed'], closure['pause_ownership']]
        aliases = []
        original_bytes = {}
        for index, source in enumerate(originals):
            raw = Path(source['path']).read_bytes()
            original_bytes[source['path']] = raw
            local = self.root / ('custody_' + str(index) + '.json')
            local.write_bytes(raw)
            aliases.append(dict(source=source, local=h.pin(local)))
            if custody:
                Path(source['path']).unlink()
        value = dict(schema='causes.native_monitor_registration_custody.v1',
                     policy=policy_ref, registration=registration_ref,
                     registered_companion_module=h.pin(old_module), local_companion_module=local_module,
                     aliases=aliases, provenance=[row['closure']['review']])
        case = SimpleNamespace(policy=policy, registration=registration, policy_ref=policy_ref,
                               registration_ref=registration_ref, auth=auth, value=value,
                               old_module=old_module, originals=originals, original_bytes=original_bytes)
        if custody:
            self.bind_custody(case)
        else:
            self.write_auth(case)
        return case

    def write_auth(self, case):
        case.auth_path = self.root / 'AUTHORIZATION.json'
        case.auth_path.write_bytes(m.encoded(case.auth))
        case.config = m.MonitorConfig(case.policy_ref, case.auth_path)

    def bind_custody(self, case, review_change=None):
        evidence = self.save('registration-custody.json', case.value)
        review = dict(accepted_native_monitor_registration_custody=True, custody=evidence,
                      **{key: case.value[key] for key in ('policy', 'registration',
                         'registered_companion_module', 'local_companion_module')})
        if review_change:
            review_change(review)
        case.auth['registration_custody'] = dict(evidence=evidence,
            review=self.save('registration-custody-review.json', review))
        self.write_auth(case)

    def test_exact_missing_three_aliases_validate_full_native_authorization(self):
        case = self.case()
        before = copy.deepcopy((case.policy, case.registration))
        registration, _, auth = m.validate_authorization(case.config)
        self.assertEqual(registration, case.registration)
        self.assertEqual(auth, case.auth)
        self.assertEqual(before, (case.policy, case.registration))
        self.assertTrue(all(not Path(ref['path']).exists() for ref in case.originals))

    def test_legacy_default_native_authorization_still_uses_original_paths(self):
        case = self.case(custody=False)
        self.assertEqual(m.validate_authorization(case.config)[0], case.registration)
        Path(case.originals[0]['path']).unlink()
        with self.assertRaises(FileNotFoundError):
            m.validate_authorization(case.config)

    def test_present_null_or_unknown_pair_is_not_default_fallback(self):
        case = self.case()
        for pair in (None, {}, {'evidence': case.auth['registration_custody']['evidence'], 'review': None, 'extra': True}):
            with self.subTest(pair=pair):
                case.auth['registration_custody'] = pair
                self.write_auth(case)
                with self.assertRaisesRegex(ValueError, 'custody_pair_required'):
                    m.validate_authorization(case.config)

    def test_missing_alias_does_not_substitute_another_file(self):
        case = self.case()
        case.value['aliases'].pop()
        self.bind_custody(case)
        with self.assertRaises(FileNotFoundError):
            m.validate_authorization(case.config)

    def test_alias_bytes_or_pin_mismatch_refuses(self):
        case = self.case()
        local = case.value['aliases'][0]['local']
        Path(local['path']).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'custody_bytes_changed'):
            m.validate_authorization(case.config)
        local.update(h.pin(local['path']))
        self.bind_custody(case)
        with self.assertRaisesRegex(ValueError, 'custody_alias_conflict'):
            m.validate_authorization(case.config)

    def test_duplicate_source_path_refuses_even_identical_row(self):
        case = self.case()
        case.value['aliases'].append(copy.deepcopy(case.value['aliases'][0]))
        self.bind_custody(case)
        with self.assertRaisesRegex(ValueError, 'custody_alias_conflict'):
            m.validate_authorization(case.config)

    def test_symlink_and_noncanonical_aliases_refuse(self):
        case = self.case()
        target = case.value['aliases'][0]['local']['path']
        link = self.root / 'alias-link.json'
        link.symlink_to(target)
        for path in (str(link), str(self.root) + '/metadata/../' + Path(target).name):
            with self.subTest(path=path):
                case.value['aliases'][0]['local']['path'] = path
                self.bind_custody(case)
                with self.assertRaisesRegex(ValueError, 'noncanonical_monitor_path'):
                    m.validate_authorization(case.config)

    def test_policy_registration_or_peer_scope_mismatch_refuses(self):
        case = self.case()
        for key in ('policy', 'registration', 'registered_companion_module', 'local_companion_module'):
            with self.subTest(key=key):
                self.bind_custody(case, lambda review: review.update({key: {'wrong': 'pin'}}))
                with self.assertRaises(ValueError):
                    m.validate_authorization(case.config)
        self.bind_custody(case, lambda review: review.update(accepted_native_monitor_registration_custody=False))
        with self.assertRaisesRegex(ValueError, 'custody_not_accepted'):
            m.validate_authorization(case.config)

    def test_registered_module_mutation_refuses(self):
        case = self.case()
        case.old_module.write_bytes(b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'custody_bytes_changed'):
            m.validate_authorization(case.config)

    def test_registered_module_repin_cannot_change_registration_identity(self):
        case = self.case()
        case.old_module.write_bytes(b'# different accepted bytes\n')
        case.value['registered_companion_module'] = h.pin(case.old_module)
        self.bind_custody(case)
        with self.assertRaisesRegex(ValueError, 'registered_monitor_source_changed'):
            m.validate_authorization(case.config)

    def test_executing_local_module_mismatch_refuses(self):
        case = self.case()
        case.value['local_companion_module'] = h.pin(case.old_module)
        self.bind_custody(case)
        with self.assertRaisesRegex(ValueError, 'custody_local_module_changed'):
            m.validate_authorization(case.config)
        case.auth['companion_module'] = h.pin(case.old_module)
        self.write_auth(case)
        with self.assertRaisesRegex(ValueError, 'monitor_source_changed'):
            m.validate_authorization(case.config)

    def test_reader_rehashes_each_alias_and_never_redirects_unlisted_refs(self):
        case = self.case()
        reader = m.registration_custody_reader(case.auth, case.policy_ref, case.registration)
        source = case.originals[0]
        self.assertEqual(reader(source), json.loads(case.original_bytes[source['path']]))
        wrong = dict(source, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'custody_source_pin_changed'):
            reader(wrong)
        with self.assertRaises(FileNotFoundError):
            reader(dict(source, path=str(self.root / 'unlisted.json')))
        Path(case.value['aliases'][0]['local']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'custody_bytes_changed'):
            reader(source)

    def test_provenance_drift_and_empty_provenance_refuse(self):
        case = self.case()
        provenance = case.value['provenance'][0]
        Path(provenance['path']).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'custody_bytes_changed'):
            m.validate_authorization(case.config)
        case.value['provenance'] = []
        self.bind_custody(case)
        with self.assertRaisesRegex(ValueError, 'custody_provenance_required'):
            m.validate_authorization(case.config)

    def test_alias_json_rejects_duplicate_keys_and_nonfinite_values(self):
        case = self.case()
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.subTest(raw=raw):
                local = Path(case.value['aliases'][0]['local']['path'])
                local.write_bytes(raw)
                source = dict(h.pin(local), path=case.originals[0]['path'])
                case.value['aliases'][0] = dict(source=source, local=h.pin(local))
                self.bind_custody(case)
                reader = m.registration_custody_reader(case.auth, case.policy_ref, case.registration)
                with self.assertRaises(ValueError):
                    reader(source)


if __name__ == '__main__':
    unittest.main()
