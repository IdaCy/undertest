"""Explicit owned-SSH transport for a supplement invocation and its saved audit.

The remote program, input and native collector remain caller-owned. This module
adds one finite connection, exact per-call custody, and fail-closed cleanup.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import types

SCHEMA = 'causes.supplement_owned_ssh_transport.v1'
ENV = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8',
           PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')


def require(value, message):
    if not value:
        raise ValueError(message)


def pin(path):
    path = Path(path); value = path.read_bytes()
    return dict(path=str(path), bytes=len(value), sha256=hashlib.sha256(value).hexdigest())


def raw(ref):
    require(pin(ref['path']) == ref, 'transport pin changed')
    return Path(ref['path']).read_bytes()


def value(ref, read):
    return json.loads(read(ref))


def destination(plan):
    argv = plan['ssh_argv']
    require(isinstance(argv, list) and len(argv) >= 2 and
            all(isinstance(arg, str) and arg and '\x00' not in arg for arg in argv),
            'transport destination argv')
    target = argv[-1]
    require(not target.startswith('-') and not any(char.isspace() for char in target),
            'transport destination')
    return target


def validate_plan(plan, read=raw):
    """Validate immutable joins without starting a process or requiring READY."""
    item = plan['control_transport']
    require(set(item) == {'schema', 'integration_source', 'helper_source',
            'helper_source_review', 'master_binding', 'python',
            'supervisor_stream_limit_bytes'}, 'transport descriptor fields')
    require(item['schema'] == SCHEMA and
            item['integration_source'] == plan['sources']['control_transport'] and
            type(item['supervisor_stream_limit_bytes']) is int and
            item['supervisor_stream_limit_bytes'] == 65536, 'transport descriptor')
    peer = value(item['helper_source_review'], read)
    require(peer['accepted_owned_ssh_control_source'] is True and
            peer['findings'] == [] and peer['source'] == item['helper_source'],
            'unaccepted transport helper')
    binding = value(item['master_binding'], read)
    require(binding['schema'] == 'shared.owned_ssh_control.v1' and
            binding['source'] == item['helper_source'] and
            binding['destination'] == destination(plan),
            'transport binding identity')
    import shlex
    command = plan['provider_interpreter'] + ' -c ' + shlex.quote(read(plan['sources']['remote']).decode())
    require(binding['remote_command'] == command, 'transport changed remote command')
    require(binding['output_directory'] == str(Path(plan['output_root'])/'control_transport'/'master'),
            'transport output escaped invocation')
    require(type(binding['maximum_lifetime_seconds']) is int and
            1 <= binding['maximum_lifetime_seconds'] <= 86400 and
            type(binding['startup_timeout_seconds']) is int and
            1 <= binding['startup_timeout_seconds'] <= 60, 'transport lifetime')
    for ref in (item['integration_source'], item['helper_source'], item['python'],
                binding['ssh'], binding['false_executable']):
        read(ref)
    return binding


def static_refs(plan, read=raw):
    binding = validate_plan(plan, read)
    item = plan['control_transport']
    return [item[key] for key in ('integration_source', 'helper_source',
            'helper_source_review', 'master_binding', 'python')] + [
                binding['ssh'], binding['false_executable']]


def load_helper(plan, read=raw):
    validate_plan(plan, read)
    ref = plan['control_transport']['helper_source']; data = read(ref)
    name = '_owned_supplement_transport_' + ref['sha256']
    helper = types.ModuleType(name); helper.__file__ = ref['path']
    sys.modules[name] = helper
    exec(compile(data, ref['path'], 'exec'), helper.__dict__)
    require(read(ref) == data, 'helper changed during import')
    return helper


def call_record(plan, ready):
    return dict(schema=SCHEMA, ready=ready)


def client_argv(plan, ready, command):
    item = plan['control_transport']
    return [item['python']['path'], '-B', item['helper_source']['path'], 'client',
            '--binding', json.dumps(item['master_binding'], sort_keys=True),
            '--ready', json.dumps(ready, sort_keys=True), destination(plan), command]


def saved_call(plan, record, read=raw):
    """Reconstruct historical transport custody without a live master check."""
    binding = validate_plan(plan, read); item = plan['control_transport']
    require(set(record) == {'schema', 'ready'} and record['schema'] == SCHEMA,
            'saved transport call schema')
    ref = record['ready']; ready = value(ref, read)
    require(ref['path'] == str(Path(binding['output_directory'])/'READY.json'),
            'saved READY path')
    helper = load_helper(plan, read)
    require(ready['schema'] == 'shared.owned_ssh_control.v1.ready' and
            ready['binding'] == item['master_binding'] and
            ready['source'] == item['helper_source'] and
            ready['master']['argv'] == helper.argv(binding, True), 'saved master identity')
    require(type(ready['master']['pid']) is int and ready['master']['pid'] > 0 and
            ready['master']['start_ticks'].isdigit() and
            ready['control_check'] == 'Master running (pid=%d)\r\n' % ready['master']['pid'],
            'saved master check')
    require(ready['socket']['mode'] == 0o600 and
            all(type(ready['socket'][k]) is int for k in ('device', 'inode', 'uid', 'mode')) and
            ready['master_socket_fds'] and ready['boot_id'], 'saved socket identity')
    return static_refs(plan, read) + [ref], client_argv(plan, ref, binding['remote_command'])


def save(path, data):
    data = (json.dumps(data, sort_keys=True, indent=2)+'\n').encode()
    require(len(data) <= 32768, 'transport document bound')
    with Path(path).open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    return pin(path)


class Session:
    """One driver-owned supervisor. A failure closes it permanently."""
    def __init__(self, plan, read=raw):
        self.plan = plan; self.read = read
        self.binding = validate_plan(plan, read); self.item = plan['control_transport']
        self.helper = load_helper(plan, read)
        self.root = Path(plan['output_root'])/'control_transport'
        self.process = None; self.ready_ref = None; self.result_ref = None
        self.streams = []; self.closed = False; self.started = False; self.close_error = None

    def start(self):
        require(not self.started and not self.closed, 'transport already started or closed')
        self.started = True
        self.root.mkdir(mode=0o700)
        try:
            require(not Path(self.binding['output_directory']).exists() and
                    not Path(self.binding['control_directory']).exists(), 'transport namespace exists')
            for name in ('supervisor.stdout', 'supervisor.stderr'):
                self.streams.append((self.root/name).open('xb'))
            parent = os.getpid()
            def limits():
                resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
                self.helper.parent_death(parent)
            argv = [self.item['python']['path'], '-B', self.item['helper_source']['path'],
                    'master', '--binding', json.dumps(self.item['master_binding'], sort_keys=True)]
            self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                stdout=self.streams[0], stderr=self.streams[1], env=ENV,
                start_new_session=True, preexec_fn=limits)
            deadline = time.monotonic() + self.binding['startup_timeout_seconds'] + 3
            path = Path(self.binding['output_directory'])/'READY.json'
            while not path.exists():
                require(self.process.poll() is None and time.monotonic() < deadline,
                        'owned master did not become ready')
                time.sleep(.05)
            self.ready_ref = pin(path)
            self.before_call()
        except BaseException as error:
            self.close('startup_failure', repr(error))
            raise

    def before_call(self):
        require(self.started and not self.closed, 'owned master unavailable; no reconnect')
        try:
            require(self.ready_ref is not None and self.process.poll() is None,
                    'owned master unavailable; no reconnect')
            for ref in static_refs(self.plan, self.read):
                self.read(ref)
            self.helper.ready(self.item['master_binding'], self.ready_ref)
            return call_record(self.plan, self.ready_ref)
        except BaseException as error:
            self.close('source_failure', repr(error))
            raise

    def close(self, reason='normal_ending', cause=None):
        if self.closed:
            require(self.close_error is None, self.close_error)
            return self.result_ref
        self.closed = True; cleanup_error = None
        try:
            if self.process is not None and self.process.poll() is None:
                if self.ready_ref is not None:
                    self.helper.stop(self.item['master_binding'], self.ready_ref)
                else:
                    self.process.terminate()
                self.process.wait(timeout=8)
        except BaseException as error:
            cleanup_error = repr(error)
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=3)
        finally:
            for stream in self.streams:
                stream.close()
        result_path = Path(self.binding['output_directory'])/'RESULT.json'
        self.result_ref = save(self.root/'CLOSED.json', dict(schema=SCHEMA+'.closed',
            at=dt.datetime.now(dt.timezone.utc).isoformat(), reason=reason, cause=cause,
            descriptor=self.item, ready=self.ready_ref, cleanup_error=cleanup_error,
            supervisor_returncode=None if self.process is None else self.process.returncode,
            master_result=pin(result_path) if result_path.exists() else None,
            master_intent=pin(Path(self.binding['output_directory'])/'INTENT.json')
                if (Path(self.binding['output_directory'])/'INTENT.json').exists() else None,
            master_stop=pin(Path(self.binding['output_directory'])/'STOP.json')
                if (Path(self.binding['output_directory'])/'STOP.json').exists() else None,
            streams=[pin(self.root/name) for name in ('supervisor.stdout','supervisor.stderr')
                     if (self.root/name).exists()], automatic_retry=False, reconnect=False))
        result = json.loads(result_path.read_bytes()) if result_path.exists() else None
        healthy = (cleanup_error is None and self.process is not None and
                   self.process.returncode == 0 and result is not None and
                   result['reason'] == 'requested_stop' and result['error'] is None and
                   result['returncode'] == 0 and result['binding'] == self.item['master_binding'] and
                   result['ready'] == self.ready_ref and result['automatic_retry'] is False)
        self.close_error = None if healthy else 'owned master cleanup failed'
        require(healthy, self.close_error)
        return self.result_ref


def saved_closed(plan, ref, ready_refs, read=raw):
    binding = validate_plan(plan, read); item = plan['control_transport']
    require(ref['path'] == str(Path(plan['output_root'])/'control_transport'/'CLOSED.json'),
            'transport closure path')
    closed = value(ref, read)
    require(closed['schema'] == SCHEMA+'.closed' and closed['descriptor'] == item and
            closed['cleanup_error'] is None and closed['supervisor_returncode'] == 0 and
            closed['automatic_retry'] is False and closed['reconnect'] is False,
            'transport cleanup not established')
    require(closed['ready'] is not None and all(r == closed['ready'] for r in ready_refs),
            'master replaced within invocation')
    saved_call(plan, call_record(plan, closed['ready']), read)
    result = value(closed['master_result'], read)
    require(closed['master_result']['path'] == str(Path(binding['output_directory'])/'RESULT.json') and
            result['binding'] == item['master_binding'] and result['ready'] == closed['ready'] and
            result['reason'] == 'requested_stop' and result['error'] is None and
            result['returncode'] == 0 and result['automatic_retry'] is False, 'master ending')
    intent = value(closed['master_intent'], read); stop = value(closed['master_stop'], read)
    ready = value(closed['ready'], read); helper = load_helper(plan, read)
    require(closed['master_intent']['path'] == str(Path(binding['output_directory'])/'INTENT.json') and
            closed['master_stop']['path'] == str(Path(binding['output_directory'])/'STOP.json') and
            intent['binding'] == stop['binding'] == item['master_binding'] and
            intent['argv'] == helper.argv(binding, True) and intent['automatic_retry'] is False and
            stop['ready'] == closed['ready'] and stop['stop'] is True, 'master start/stop custody')
    clocks = [dt.datetime.fromisoformat(x['at']) for x in (intent, ready, stop, result, closed)]
    require(all(x.tzinfo is not None for x in clocks) and clocks == sorted(clocks),
            'master saved clock order')
    require((clocks[3]-clocks[0]).total_seconds() <= binding['maximum_lifetime_seconds'] + 30,
            'master lifetime exceeded')
    require(closed['reason'] in ('normal_ending', 'remote_failure', 'source_failure',
            'guard_failure', 'capture_failure', 'driver_exit'), 'master closure reason')
    require([s['path'] for s in closed['streams']] == [str(Path(plan['output_root'])/'control_transport'/n)
            for n in ('supervisor.stdout', 'supervisor.stderr')], 'supervisor stream roster')
    require(json.loads(read(closed['streams'][0])) == result and
            read(closed['streams'][1]) == b'', 'unexpected transport supervisor stream')
    for stream in [result['stdout'], result['stderr']]:
        require(read(stream) == b'', 'unexpected transport SSH stream')
    return dict(closed=ref, ready=closed['ready'], master_result=closed['master_result'],
                ready_at=ready['at'], stopped_at=stop['at'], reason=closed['reason'],
                one_master=True, automatic_retry=False, reconnect=False)
