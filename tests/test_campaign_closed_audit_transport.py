"""Saved transport integration using synthetic local evidence, never SSH."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest

from causes import campaign_supplement_closed_audit as audit
from causes import campaign_supplement_transport as transport


class SavedTransportFixture:
    def __init__(self, root, *, executable_bytes=64):
        self.root = Path(root).resolve()
        self.out = self.root / 'output'
        self.out.mkdir()
        self.helper = self.put('helper.py',
            b'def argv(binding, master): return ["ssh", "synthetic-master"]\n')
        sources = {name: self.put(name + '.py', b'# synthetic source\n')
                   for name in ('remote', 'provider_status', 'collector', 'exact_bytes')}
        sources['control_transport'] = audit.pin(Path(transport.__file__).resolve())
        self.provider_plan = {'native_source': self.put('native.py', b'# synthetic native\n')}
        self.plan = dict(output_root=str(self.out), sources=sources,
            contract=self.put('contract.json', {}),
            provider_intent=self.put('provider.json', {'request': {'plan': self.provider_plan}}),
            completed_actor_custody={'actual': self.put('custody.json', {'retired_actors': []})},
            provider_identity={'synthetic': True}, remote_pause_paths=[],
            ssh_argv=['ssh', 'synthetic.invalid'], provider_interpreter='python -B',
            maximum_admission_attempts=100, maximum_captures=10)
        self.binding = dict(schema='shared.owned_ssh_control.v1', source=self.helper,
            destination='synthetic.invalid', output_directory=str(self.out/'control_transport/master'),
            control_directory=str(self.root/'control'), maximum_lifetime_seconds=60,
            startup_timeout_seconds=1,
            ssh=self.put('ssh', b's' * executable_bytes),
            false_executable=self.put('false', b'f' * executable_bytes),
            remote_command='python -B -c ' + shlex.quote('# synthetic source\n'))
        self.binding_ref = self.put('binding.json', self.binding)
        self.plan['control_transport'] = dict(schema=transport.SCHEMA,
            integration_source=sources['control_transport'], helper_source=self.helper,
            helper_source_review=self.put('peer.json', dict(
                accepted_owned_ssh_control_source=True, source=self.helper, findings=[])),
            master_binding=self.binding_ref, python=self.put('python', b'p' * executable_bytes),
            supervisor_stream_limit_bytes=65536)
        self.mods = dict(core=SimpleNamespace(), direct=SimpleNamespace(), transport=transport)
        self.ready_body = dict(schema='shared.owned_ssh_control.v1.ready',
            at='2026-01-01T00:00:01+00:00', binding=self.binding_ref, source=self.helper,
            master=dict(pid=123, start_ticks='456', argv=['ssh', 'synthetic-master']),
            control_check='Master running (pid=123)\r\n',
            socket=dict(device=1, inode=2, uid=1000, mode=0o600),
            master_socket_fds=['socket:[3]'], boot_id='synthetic-boot')
        self.ready = self.put('output/control_transport/master/READY.json', self.ready_body)
        self.files = []

    def put(self, name, value):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else audit.encoded(value))
        return audit.pin(path)

    def make_calls(self, count):
        self.files = []
        for number in range(1, count + 1):
            refs = [self.plan['sources'][k] for k in
                    ('remote', 'provider_status', 'collector', 'exact_bytes')]
            refs += [self.plan['provider_intent'], self.provider_plan['native_source']]
            refs += transport.static_refs(self.plan) + [self.ready]
            envelope = dict(provider_plan=self.provider_plan,
                runtime_identity=self.plan['provider_identity'],
                helper_source=self.plan['sources']['provider_status'],
                helper_base64=base64.b64encode(b'# synthetic source\n').decode(),
                native_base64=base64.b64encode(b'# synthetic native\n').decode(),
                retired_actors=[], remote_pause_paths=[], full=False)
            payload = audit.encoded(envelope)[:-1]
            intent = dict(full=False, control_transport=transport.call_record(self.plan, self.ready),
                source=self.plan['sources']['remote'], sources=refs, maximum_invocations=1,
                automatic_retry=False, at='2026-01-01T00:00:02+00:00',
                argv=transport.client_argv(self.plan, self.ready, self.binding['remote_command']),
                payload_bytes=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest())
            prefix = 'output/remote_records/call_%06d/' % number
            ir = self.put(prefix+'000001_intent', intent)
            stdout = self.put(prefix+'000002_stdout', b'{}\n')
            stderr = self.put(prefix+'000003_stderr', b'')
            er = self.put(prefix+'000004_execution', dict(intent=ir, output=stdout, stderr=stderr,
                automatic_retry=False, model_calls=0, source_error=None,
                transport=dict(complete=True, returncode=0)))
            self.files.extend([ir, stdout, stderr, er])
        return self.files

    def close(self, **changes):
        prefix = 'output/control_transport/master/'
        intent = self.put(prefix+'INTENT.json', dict(at='2026-01-01T00:00:00+00:00',
            binding=self.binding_ref, argv=['ssh', 'synthetic-master'], automatic_retry=False))
        stop = self.put(prefix+'STOP.json', dict(at='2026-01-01T00:00:03+00:00',
            binding=self.binding_ref, ready=self.ready, stop=True))
        result_body = dict(at='2026-01-01T00:00:04+00:00', binding=self.binding_ref,
            ready=self.ready, reason='requested_stop', error=None, returncode=0,
            automatic_retry=False, stdout=self.put(prefix+'stdout', b''),
            stderr=self.put(prefix+'stderr', b''))
        result = self.put(prefix+'RESULT.json', result_body)
        streams = [self.put('output/control_transport/supervisor.stdout', result_body),
                   self.put('output/control_transport/supervisor.stderr', b'')]
        closed = dict(schema=transport.SCHEMA+'.closed', at='2026-01-01T00:00:05+00:00',
            descriptor=self.plan['control_transport'], cleanup_error=None,
            supervisor_returncode=0, automatic_retry=False, reconnect=False,
            ready=self.ready, master_result=result, master_intent=intent, master_stop=stop,
            reason='normal_ending', streams=streams)
        closed.update(changes)
        return self.put('output/control_transport/CLOSED.json', closed)

    def auditor(self, **kwargs):
        return audit.SavedAudit(self.plan, {}, self.mods, b'', [], **kwargs)


class SavedTransportAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = SavedTransportFixture(tmp.name)
        self.files = self.f.make_calls(2)
        self.closed = self.f.close()

    def test_saved_calls_and_closure_preserve_accounting(self):
        a = self.f.auditor()
        result = a.remote_accounting(self.files, 2, self.closed)
        self.assertEqual(result['calls'], 2)
        self.assertEqual([r['directory'] for r in result['rows']], ['call_000001', 'call_000002'])
        self.assertEqual(a.control_ready_refs, [self.f.ready, self.f.ready])
        self.assertEqual(result['control_transport']['closed'], self.closed)
        self.assertTrue(result['control_transport']['one_master'])
        self.assertTrue(result['failed_and_unclaimed_calls_retained'])

    def test_missing_transport_module_refuses(self):
        self.f.mods.pop('transport')
        with self.assertRaisesRegex(ValueError, 'exact_saved_transport_module'):
            self.f.auditor()

    def test_failed_closure_refuses(self):
        bad = self.f.close(cleanup_error='synthetic timeout')
        with self.assertRaisesRegex(ValueError, 'cleanup not established'):
            self.f.auditor().remote_accounting(self.files, 2, bad)

    def test_call_outside_original_lifetime_refuses(self):
        ref = self.files[0]
        value = json.loads(Path(ref['path']).read_bytes())
        value['at'] = '2026-01-01T00:00:04+00:00'
        self.files[0] = self.f.put(Path(ref['path']).relative_to(self.f.root), value)
        er = self.files[3]
        execution = json.loads(Path(er['path']).read_bytes())
        execution['intent'] = self.files[0]
        self.files[3] = self.f.put(Path(er['path']).relative_to(self.f.root), execution)
        with self.assertRaisesRegex(ValueError, 'transport_call_outside_owned_lifetime'):
            self.f.auditor().remote_accounting(self.files, 2, self.closed)

    def test_undeclared_transport_cannot_be_added_to_plain_plan(self):
        self.f.plan.pop('control_transport')
        a = self.f.auditor()
        intent = json.loads(Path(self.files[0]['path']).read_bytes())
        with self.assertRaisesRegex(ValueError, 'undeclared_transport'):
            a.remote_intent(intent)


if __name__ == '__main__':
    unittest.main()
