import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]/'src/causes/campaign_supplement_transport.py'
spec = importlib.util.spec_from_file_location('tested_transport', SOURCE)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class Fixture:
    def __init__(self, root):
        self.root = Path(root); self.out = self.root/'output'; self.out.mkdir()
        def put(name, data):
            path = self.root/name
            path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
            return m.pin(path)
        self.put = put
        self.helper = put('helper.py', b'# accepted, separately qualified helper\n')
        self.peer = put('peer.json', dict(accepted_owned_ssh_control_source=True,
                         source=self.helper, findings=[]))
        self.remote = put('remote.py', b'print("unchanged native command")\n')
        self.python = put('python', b'python executable identity')
        self.ssh = put('ssh', b'ssh executable identity')
        self.false = put('false', b'false executable identity')
        import shlex
        self.binding = dict(schema='shared.owned_ssh_control.v1', source=self.helper,
            destination='approved-test', output_directory=str(self.out/'control_transport/master'),
            control_directory=str(self.root/'control'), maximum_lifetime_seconds=3600,
            startup_timeout_seconds=1, ssh=self.ssh, false_executable=self.false,
            remote_command='python -B -c '+shlex.quote(Path(self.remote['path']).read_text()))
        self.binding_ref = put('binding.json', self.binding)
        self.plan = dict(output_root=str(self.out), ssh_argv=['ssh','approved-test'],
            provider_interpreter='python -B', sources=dict(remote=self.remote, control_transport=m.pin(SOURCE)),
            control_transport=dict(schema=m.SCHEMA, integration_source=m.pin(SOURCE),
                helper_source=self.helper, helper_source_review=self.peer,
                master_binding=self.binding_ref, python=self.python,
                supervisor_stream_limit_bytes=65536))

    def rebind(self):
        self.binding_ref = self.put('binding.json', self.binding)
        self.plan['control_transport']['master_binding'] = self.binding_ref

    def saved(self):
        base = Path(self.binding['output_directory']); base.mkdir(parents=True)
        helper = mock.Mock()
        helper.argv.return_value = ['ssh','exact-master']
        def write(name, data):
            p=base/name; p.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
            return m.pin(p)
        ready = dict(schema='shared.owned_ssh_control.v1.ready', at='2026-01-01T00:00:01+00:00',
            binding=self.binding_ref, source=self.helper, master=dict(pid=123,start_ticks='456',
            argv=['ssh','exact-master']),control_check='Master running (pid=123)\r\n',
            socket=dict(device=1,inode=2,uid=1000,mode=0o600),master_socket_fds=['socket:[3]'],boot_id='boot')
        rr=write('READY.json',ready)
        ir=write('INTENT.json',dict(at='2026-01-01T00:00:00+00:00',binding=self.binding_ref,
            argv=['ssh','exact-master'],automatic_retry=False))
        stop=write('STOP.json',dict(at='2026-01-01T00:00:02+00:00',binding=self.binding_ref,ready=rr,stop=True))
        result=write('RESULT.json',dict(at='2026-01-01T00:00:03+00:00',binding=self.binding_ref,
            ready=rr,reason='requested_stop',error=None,returncode=0,automatic_retry=False,
            stdout=write('stdout.bin',b''),stderr=write('stderr.bin',b'')))
        streams=[]
        for name in ('supervisor.stdout','supervisor.stderr'):
            p=base.parent/name
            p.write_bytes(Path(result['path']).read_bytes() if name.endswith('stdout') else b'')
            streams.append(m.pin(p))
        closed=dict(schema=m.SCHEMA+'.closed',at='2026-01-01T00:00:04+00:00',
            descriptor=self.plan['control_transport'],ready=rr,cleanup_error=None,supervisor_returncode=0,
            automatic_retry=False,reconnect=False,reason='normal_ending',master_result=result,
            master_intent=ir,master_stop=stop,streams=streams)
        cr=m.save(base.parent/'CLOSED.json',closed)
        return helper,rr,cr,closed


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.f=Fixture(self.tmp.name)

    def test_static_roster_has_no_future_ready(self):
        refs=m.static_refs(self.f.plan)
        self.assertEqual(len(refs),7)
        self.assertEqual(refs[-2:],[self.f.ssh,self.f.false])
        self.assertFalse(any('READY' in r['path'] for r in refs))

    def test_unaccepted_peer_prevents_import(self):
        self.f.plan['control_transport']['helper_source_review']=self.f.put('badpeer.json',
            dict(accepted_owned_ssh_control_source=False,source=self.f.helper,findings=[]))
        with mock.patch.object(m.types,'ModuleType') as imported, self.assertRaises(ValueError):
            m.load_helper(self.f.plan)
        imported.assert_not_called()

    def test_changed_native_command_refuses(self):
        self.f.binding['remote_command']+=' changed';self.f.rebind()
        with self.assertRaisesRegex(ValueError,'command'):m.validate_plan(self.f.plan)

    def test_foreign_output_refuses(self):
        self.f.binding['output_directory']='/elsewhere/master';self.f.rebind()
        with self.assertRaisesRegex(ValueError,'output'):m.validate_plan(self.f.plan)

    def test_noninteger_or_unbounded_lifetime_refuses(self):
        for bad in (True,3600.0,0,86401):
            self.f.binding['maximum_lifetime_seconds']=bad;self.f.rebind()
            with self.assertRaisesRegex(ValueError,'lifetime'):m.validate_plan(self.f.plan)

    def test_descriptor_extra_ready_refuses(self):
        self.f.plan['control_transport']['ready']={'future':True}
        with self.assertRaisesRegex(ValueError,'fields'):m.validate_plan(self.f.plan)

    def test_source_mutation_refuses(self):
        Path(self.f.helper['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'pin'):m.static_refs(self.f.plan)

    def test_client_argv_preserves_entire_command(self):
        ready=dict(path='/private/READY.json',bytes=12,sha256='a'*64)
        command=self.f.binding['remote_command']
        argv=m.client_argv(self.f.plan,ready,command)
        self.assertEqual(argv[-2:],['approved-test',command])
        self.assertEqual(json.loads(argv[argv.index('--ready')+1]),ready)

    def test_destination_comes_from_matching_frozen_plan_and_binding(self):
        self.f.binding['destination'] = 'second-approved-host'
        self.f.plan['ssh_argv'][-1] = 'second-approved-host'
        self.f.rebind()
        self.assertEqual(m.validate_plan(self.f.plan)['destination'], 'second-approved-host')
        ready = dict(path='/saved/READY.json',bytes=12,sha256='a'*64)
        self.assertEqual(m.client_argv(self.f.plan,ready,'unchanged command')[-2:],
                         ['second-approved-host','unchanged command'])

    def test_plan_binding_destination_mismatch_refuses(self):
        self.f.plan['ssh_argv'][-1] = 'another-host'
        with self.assertRaisesRegex(ValueError,'binding identity'):
            m.validate_plan(self.f.plan)

    def test_missing_or_option_destination_refuses(self):
        for argv in ([], ['ssh'], 'ssh approved-test', ['ssh', ''], ['ssh', None],
                     ['ssh', '-oProxyCommand=anything'], ['ssh', 'two hosts'],
                     ['ssh', 'bad\x00host']):
            with self.subTest(argv=argv):
                self.f.plan['ssh_argv'] = argv
                with self.assertRaisesRegex(ValueError,'destination'):
                    m.client_argv(self.f.plan, {}, 'unchanged command')

    def test_saved_call_adds_exact_seven_and_ready(self):
        helper,ready,closed,_=self.f.saved()
        with mock.patch.object(m,'load_helper',return_value=helper):
            refs,argv=m.saved_call(self.f.plan,m.call_record(self.f.plan,ready))
        self.assertEqual(refs,m.static_refs(self.f.plan)+[ready])
        self.assertEqual(argv[-1],self.f.binding['remote_command'])
        helper.ready.assert_not_called()

    def test_saved_normal_cleanup(self):
        helper,ready,closed,_=self.f.saved()
        with mock.patch.object(m,'load_helper',return_value=helper):
            result=m.saved_closed(self.f.plan,closed,[ready,ready])
        self.assertTrue(result['one_master']);helper.stop.assert_not_called()

    def test_switched_master_refuses(self):
        helper,ready,closed,_=self.f.saved();other=dict(ready,sha256='b'*64)
        with mock.patch.object(m,'load_helper',return_value=helper),self.assertRaisesRegex(ValueError,'replaced'):
            m.saved_closed(self.f.plan,closed,[ready,other])

    def test_cleanup_failure_is_not_accepted(self):
        helper,ready,closed,body=self.f.saved();body['cleanup_error']='timeout'
        p=Path(closed['path']);p.write_text(json.dumps(body));closed=m.pin(p)
        with mock.patch.object(m,'load_helper',return_value=helper),self.assertRaisesRegex(ValueError,'cleanup'):
            m.saved_closed(self.f.plan,closed,[ready])

    def test_nonempty_supervisor_stream_refuses(self):
        helper,ready,closed,body=self.f.saved();p=Path(body['streams'][1]['path']);p.write_bytes(b'error')
        body['streams'][1]=m.pin(p);p=Path(closed['path']);p.write_text(json.dumps(body));closed=m.pin(p)
        with mock.patch.object(m,'load_helper',return_value=helper),self.assertRaisesRegex(ValueError,'stream'):
            m.saved_closed(self.f.plan,closed,[ready])

    def test_late_call_can_be_compared_to_saved_stop(self):
        helper,ready,closed,_=self.f.saved()
        with mock.patch.object(m,'load_helper',return_value=helper):
            c=m.saved_closed(self.f.plan,closed,[ready])
        self.assertEqual(c['ready_at'],'2026-01-01T00:00:01+00:00')
        self.assertEqual(c['stopped_at'],'2026-01-01T00:00:02+00:00')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.f=Fixture(self.tmp.name);self.helper=mock.Mock();self.process=mock.Mock()
        self.process.returncode=None;self.process.poll.side_effect=lambda:self.process.returncode
        self.process.wait.side_effect=lambda timeout:setattr(self.process,'returncode',0)
        self.helper_patch=mock.patch.object(m,'load_helper',return_value=self.helper)
        self.helper_patch.start();self.addCleanup(self.helper_patch.stop)
        self.popen=mock.patch.object(m.subprocess,'Popen',side_effect=self.spawn).start()
        self.addCleanup(mock.patch.stopall)
        self.helper.stop.side_effect=self.stop

    def spawn(self, argv, **kwargs):
        self.spawn_kwargs=kwargs
        base=Path(self.f.binding['output_directory']);base.mkdir()
        (base/'READY.json').write_text('{"synthetic":true}')
        return self.process

    def stop(self, binding, ready):
        base=Path(self.f.binding['output_directory'])
        result=dict(reason='requested_stop',error=None,returncode=0,binding=binding,
                    ready=ready,automatic_retry=False)
        (base/'RESULT.json').write_text(json.dumps(result))
        self.spawn_kwargs['stdout'].write(json.dumps(result).encode())

    def test_one_start_stop_reap_and_no_restart(self):
        session=m.Session(self.f.plan);session.start();session.before_call();ref=session.close()
        self.assertEqual(session.close(),ref)
        self.popen.assert_called_once();self.helper.stop.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=8)
        self.assertTrue(self.spawn_kwargs['start_new_session'])
        self.assertEqual(self.spawn_kwargs['env'],m.ENV)
        self.assertTrue(all(s.closed for s in session.streams))
        with self.assertRaisesRegex(ValueError,'closed'):session.start()
        with self.assertRaisesRegex(ValueError,'reconnect'):session.before_call()

    def test_ready_failure_stops_before_reuse(self):
        session=m.Session(self.f.plan);session.start()
        self.helper.ready.side_effect=ValueError('socket lost')
        with self.assertRaisesRegex(ValueError,'socket lost'):session.before_call()
        self.assertTrue(session.closed);self.helper.stop.assert_called_once()
        self.assertIn('socket lost',json.loads(Path(session.result_ref['path']).read_bytes())['cause'])
        with self.assertRaises(ValueError):session.before_call()
        self.popen.assert_called_once()

    def test_source_change_stops_before_reuse(self):
        session=m.Session(self.f.plan);session.start()
        Path(self.f.remote['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'pin'):session.before_call()
        self.assertTrue(session.closed);self.helper.stop.assert_called_once()

    def test_spawn_failure_retained_without_retry(self):
        self.popen.side_effect=OSError('spawn refused');session=m.Session(self.f.plan)
        with self.assertRaises(ValueError):session.start()
        record=json.loads(Path(session.result_ref['path']).read_bytes())
        self.assertIn('spawn refused',record['cause']);self.assertIsNone(record['supervisor_returncode'])
        self.assertTrue(all(s.closed for s in session.streams));self.popen.assert_called_once()

    def test_failed_stop_falls_back_only_to_owned_cleanup(self):
        session=m.Session(self.f.plan);session.start()
        self.helper.stop.side_effect=ValueError('owned stop refused')
        with self.assertRaisesRegex(ValueError,'cleanup'):session.close('remote_failure')
        self.process.terminate.assert_called_once();self.process.wait.assert_called_once()
        self.assertTrue(session.closed);self.popen.assert_called_once()
        with self.assertRaisesRegex(ValueError,'cleanup'):session.close()

    def test_expired_master_cannot_be_reported_normal(self):
        session=m.Session(self.f.plan);session.start()
        original=self.stop
        def expired(binding,ready):
            original(binding,ready);p=Path(self.f.binding['output_directory'])/'RESULT.json'
            result=json.loads(p.read_bytes());result['reason']='lifetime_expired';p.write_text(json.dumps(result))
        self.helper.stop.side_effect=expired
        with self.assertRaisesRegex(ValueError,'cleanup'):session.close()
        self.assertTrue(session.closed)


if __name__=='__main__':unittest.main()
