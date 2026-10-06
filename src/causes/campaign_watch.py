"""Run an explicitly configured observation schedule under one local lock."""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('noncanonical_watcher_path')
    return path


def validate(config):
    if config['schema'] != 'causes.campaign_watch_config.v1': raise ValueError('watcher_config_schema')
    directory = canonical(config['directory']); scripts = canonical(config['script_directory'])
    if directory != scripts/'watch': raise ValueError('watcher_directory_scope')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != config['watcher_sha256']:
        raise ValueError('watcher_source_changed')
    if type(config['interval_seconds']) is not int or config['interval_seconds'] <= 0:
        raise ValueError('invalid_watcher_interval')
    if type(config['periodic']['every']) is not int or config['periodic']['every'] <= 0:
        raise ValueError('invalid_periodic_interval')
    rows = config['checks'] + [config['periodic']['check']]
    if not config['checks'] or len({row['script'] for row in rows}) != len(rows): raise ValueError('distinct_watcher_checks_required')
    for row in rows:
        if re.fullmatch(r'[A-Za-z0-9_]+\.py',row['script']) is None: raise ValueError('invalid_watcher_script')
        if type(row['timeout_seconds']) is not int or row['timeout_seconds'] <= 0: raise ValueError('invalid_watcher_timeout')
        path = canonical(scripts/row['script']); raw = path.read_bytes()
        if row['source'] != {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}:
            raise ValueError('watcher_check_source_changed')
    return directory,scripts


def execute_check(scripts, iteration, row):
    script = row['script']
    command = [sys.executable,str(scripts/script)]
    with (iteration/(script+'.stdout')).open('xb') as out, (iteration/(script+'.stderr')).open('xb') as err:
        process = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
        try:
            return {'script':script,'returncode':process.wait(timeout=row['timeout_seconds']),'timeout':False}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait()
            return {'script':script,'returncode':process.returncode,'timeout':True}


def run(config):
    directory,scripts = validate(config)
    directory.mkdir(exist_ok=True)
    with (directory/'watch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        identity = {'pid':os.getpid(),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'interval_seconds':config['interval_seconds'],
            'actions':['read-only job/GPU/serving observations','additive completed-run synchronization'],
            'job_service_mutations':False,'stop_mechanism':str(directory/'STOP'),
            'configuration_sha256':hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()}
        with (directory/('START_'+str(os.getpid())+'.json')).open('x') as stream:json.dump(identity,stream,indent=2)
        cycle = 0
        while not (directory/'STOP').exists():
            started = time.monotonic()
            validate(config)
            iteration = directory/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
            iteration.mkdir()
            checks = list(config['checks'])
            if cycle % config['periodic']['every'] == 0:checks.append(config['periodic']['check'])
            results = [execute_check(scripts,iteration,row) for row in checks]
            with (iteration/'RESULT.json').open('x') as stream:
                json.dump({'cycle':cycle,'ended_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':results},stream,indent=2)
            cycle += 1
            while time.monotonic()-started < config['interval_seconds'] and not (directory/'STOP').exists():
                time.sleep(min(5,config['interval_seconds']-(time.monotonic()-started)))


def main():
    if len(sys.argv) != 1:raise ValueError('watcher_accepts_only_adjacent_configuration')
    source = canonical(Path(__file__).absolute())
    path = source.with_name(source.stem+'.config.json')
    def unique(pairs):
        value = {}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate_watcher_config_key')
            value[key] = item
        return value
    def constant(value):raise ValueError('nonfinite_watcher_config')
    config = json.loads(canonical(path).read_bytes(),object_pairs_hook=unique,parse_constant=constant)
    run(config)


if __name__ == '__main__':
    main()
