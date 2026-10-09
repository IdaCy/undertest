from pathlib import Path, PurePosixPath
from dataclasses import dataclass
from collections.abc import Callable, Mapping
import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid


@dataclass(frozen=True)
class SnapshotConfig:
    host: str
    remote_base: str
    roots: tuple[str, ...]
    destination: Path
    free_space_floor_bytes: int = 80 * 1024**3
    limit: int = 128

    def validate(self):
        if not isinstance(self.host, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.host) is None:
            raise ValueError('invalid_snapshot_host')
        remote = PurePosixPath(self.remote_base)
        if not remote.is_absolute() or str(remote) != self.remote_base or '..' in remote.parts:
            raise ValueError('noncanonical_remote_base')
        if type(self.roots) is not tuple or not self.roots or len(set(self.roots)) != len(self.roots):
            raise ValueError('explicit_distinct_snapshot_roots_required')
        for root in self.roots:
            relative_path(root)
        if type(self.limit) is not int or self.limit <= 0 or type(self.free_space_floor_bytes) is not int or self.free_space_floor_bytes < 0:
            raise ValueError('invalid_snapshot_resource_limit')
        canonical_path(self.destination)


def relative_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('invalid_relative_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in ('.', '..') for part in path.parts) or not path.parts:
        raise ValueError('noncanonical_relative_path')
    return path


def canonical_path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('noncanonical_snapshot_path')
    return path


def checked_authorization(config, authorize):
    value = authorize(config)
    if not isinstance(value, Mapping) or value.get('host') != config.host or value.get('remote_base') != config.remote_base or value.get('roots') != list(config.roots) or not value.get('evidence'):
        raise ValueError('snapshot_authorization_scope_mismatch')
    # The caller validates its registration policy and returns immutable evidence.
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


REMOTE_CODE = r'''
from pathlib import Path, PurePosixPath
import datetime,hashlib,json,os,stat,sys
request=json.load(sys.stdin)
base=Path(request['base'])
accepted=set(request['accepted'])
rows=[];skipped=[]
excluded={'upper','work','merged','reflink_workspace','pilot_copy_workspace'}
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
for root in request['roots']:
 rel=PurePosixPath(root)
 if rel.is_absolute() or str(rel)!=root or any(x in ('.','..') for x in rel.parts) or not rel.parts:
  raise ValueError('noncanonical root')
 group=base/root
 if any(p.is_symlink() for p in (group,*group.parents)):raise ValueError('symlink root')
 if not group.is_dir():
  skipped.append({'root':root,'reason':'root_not_present'});continue
 for run in sorted(group.iterdir()):
  key=str(run.relative_to(base))
  if key in accepted:continue
  if run.is_symlink() or not run.is_dir():continue
  summary=run/'summary.json'
  if not summary.is_file() or summary.is_symlink():continue
  data=json.loads(summary.read_text())
  closure=data.get('sandbox_closure',{})
  if closure.get('state')!='closed' or closure.get('overlay_mounted') is not False:
   skipped.append({'run':key,'reason':'sandbox_not_confirmed_closed'});continue
  initial_summary_sha=digest(summary)
  files=[];omitted=[]
  for directory,dirs,names in os.walk(run,followlinks=False):
   folder=Path(directory)
   kept=[]
   for name in dirs:
    p=folder/name
    r=p.relative_to(run)
    if p.is_symlink() or (r.parts[:2]==('sandbox','sandbox') and len(r.parts)==3 and name in excluded):
     omitted.append({'path':str(r),'reason':'workspace_tree_or_symlink'})
    else:kept.append(name)
   dirs[:]=kept
   for name in names:
    p=folder/name;r=str(p.relative_to(run));before=p.lstat()
    if not stat.S_ISREG(before.st_mode):
     omitted.append({'path':r,'reason':'not_regular_file'});continue
    value=digest(p);after=p.lstat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
     raise RuntimeError('completed file changed during manifest: '+key+'/'+r)
    files.append({'path':r,'bytes':before.st_size,'sha256':value,'mtime_ns':before.st_mtime_ns,'mode':stat.S_IMODE(before.st_mode)})
  if digest(summary)!=initial_summary_sha:raise RuntimeError('completed summary changed')
  rows.append({'run':key,'summary_sha256':initial_summary_sha,'files':files,'omitted':omitted,'bytes':sum(x['bytes'] for x in files),'finished_at':data.get('finished_at')})
  if len(rows)>=request['limit']:break
 if len(rows)>=request['limit']:break
print(json.dumps({'schema':'caus.completed_snapshot_manifest.v1','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':request['host'],'base':str(base),'runs':rows,'skipped':skipped}))
'''

def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def durable_tree(root):
    for directory, _, names in os.walk(root, topdown=False, followlinks=False):
        folder = Path(directory)
        for name in names:
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Unexpected nonregular local snapshot file')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        fsync_directory(folder)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_manifest(manifest, config, accepted):
    if manifest.get('schema') != 'caus.completed_snapshot_manifest.v1' or manifest.get('host') != config.host or manifest.get('base') != config.remote_base:
        raise ValueError('remote_manifest_scope_mismatch')
    rows = manifest['runs']
    if not isinstance(rows, list) or len(rows) > config.limit:
        raise ValueError('invalid_manifest_run_count')
    seen = set()
    for row in rows:
        run = relative_path(row['run'])
        if str(run.parent) not in config.roots or row['run'] in seen or row['run'] in accepted:
            raise ValueError('invalid_or_repeated_manifest_run')
        seen.add(row['run'])
        files = {}
        for item in row['files']:
            relative_path(item['path'])
            if item['path'] in files or type(item['bytes']) is not int or item['bytes'] < 0 or not isinstance(item['sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None:
                raise ValueError('invalid_or_repeated_manifest_file')
            files[item['path']] = item
        if 'summary.json' not in files or files['summary.json']['sha256'] != row['summary_sha256'] or type(row['bytes']) is not int or row['bytes'] != sum(item['bytes'] for item in files.values()):
            raise ValueError('manifest_summary_or_size_mismatch')


def synchronize_completed(config: SnapshotConfig, authorize: Callable):
    config.validate()
    authorization = checked_authorization(config, authorize)
    destination = Path(config.destination)
    destination.mkdir(parents=True, exist_ok=True)
    with canonical_path(destination / 'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _synchronize_under_lock(config, authorize, authorization)


def _synchronize_under_lock(config, authorize, authorization):
    destination = Path(config.destination)
    index = canonical_path(destination / 'accepted.jsonl')
    accepted = []
    if index.exists():
        raw_index = index.read_bytes()
        if raw_index and not raw_index.endswith(b'\n'):
            raise ValueError('unterminated_snapshot_index')
        accepted = [json.loads(line)['run'] for line in raw_index.splitlines() if line]
    if len(set(accepted)) != len(accepted) or any(str(relative_path(run).parent) not in config.roots for run in accepted):
        raise ValueError('invalid_or_repeated_accepted_run')
    attempts = canonical_path(destination / 'attempts')
    attempt = attempts / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True, exist_ok=False)
    request = {'host': config.host, 'base': config.remote_base, 'roots': list(config.roots), 'accepted': accepted, 'limit': config.limit}
    save(attempt / 'AUTHORIZATION.json', authorization)
    save(attempt / 'REQUEST.json', request)
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', config.host, 'python3', '-c', shlex.quote(REMOTE_CODE)]
    process = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, timeout=120)
    (attempt / 'remote.stderr').write_text(process.stderr)
    (attempt / 'remote.stdout').write_text(process.stdout)
    if process.returncode:
        raise RuntimeError('remote manifest failed; retained stderr at ' + str(attempt))
    manifest = json.loads(process.stdout)
    save(attempt / 'MANIFEST.json', manifest)
    validate_manifest(manifest, config, set(accepted))
    required = sum(row['bytes'] for row in manifest['runs'])
    if shutil.disk_usage(destination).free < required + config.free_space_floor_bytes:
        raise RuntimeError('configured snapshot free-space floor would be crossed')
    paths = []
    for row in manifest['runs']:
        for item in row['files']:
            full = PurePosixPath(row['run']) / item['path']
            if full.is_absolute() or any(part in ('.', '..') for part in full.parts):
                raise ValueError('invalid manifest path')
            paths.append(str(full))
    listing = attempt / 'FILES.nul'
    listing.write_bytes(b''.join(path.encode() + b'\0' for path in paths))
    data = attempt / 'data'
    data.mkdir()
    if paths:
        command = ['rsync', '-rt', '--chmod=Du+rwx,Fu+rw', '--from0', '--files-from=' + str(listing), '--no-links', '--no-specials', '--no-devices', '--protect-args', '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=10', config.host + ':' + config.remote_base + '/', str(data) + '/']
        with (attempt / 'rsync.stdout').open('xb') as out, (attempt / 'rsync.stderr').open('xb') as err:
            process = subprocess.run(command, stdout=out, stderr=err, timeout=300)
        if process.returncode:
            raise RuntimeError('rsync failed; partial attempt retained at ' + str(attempt))
    receipts = []
    for row in manifest['runs']:
        for item in row['files']:
            path = canonical_path(data / row['run'] / item['path'])
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
                raise RuntimeError('snapshot verification failed: ' + str(path))
        receipt = {'run': row['run'], 'path': str(data / row['run']), 'manifest': str(attempt / 'MANIFEST.json'), 'manifest_sha256': digest(attempt / 'MANIFEST.json'), 'files': len(row['files']), 'bytes': row['bytes'], 'summary_sha256': row['summary_sha256'], 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        receipts.append(receipt)
    save(attempt / 'VERIFIED.json', {'schema': 'caus.completed_snapshot_verified.v1', 'runs': receipts, 'scope': 'regular evidence files; workspace trees excluded; checkpoint TARs retained as opaque bytes', 'local_mode_derivation': 'owner read/write access added for snapshot inspection; original file mode recorded in manifest; opaque TAR member modes unchanged'})
    durable_tree(attempt)
    for directory in (attempt.parent, destination, destination.parent):
        fsync_directory(directory)
    if json.dumps(checked_authorization(config, authorize), sort_keys=True, allow_nan=False) != json.dumps(authorization, sort_keys=True, allow_nan=False):
        raise RuntimeError('snapshot_authorization_changed_before_acceptance')
    with index.open('a') as stream:
        for receipt in receipts:
            stream.write(json.dumps(receipt) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(index.parent)
    return {'attempt': str(attempt), 'verified_runs': len(receipts), 'verified_bytes': required, 'skipped': manifest['skipped']}
