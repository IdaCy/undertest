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


READER_INPUT_FILES = ('allocation.json', 'spec.json', 'variant.json', 'model.json',
                      'summary.json', 'initial_context.json', 'early_context.json', 'end_context.json')
READER_INPUT_FILE_LIMIT = 64 * 1024**2
READER_INPUT_PASS_LIMIT = 256 * 1024**2
EARLY_SELF_PROBE_FILES = READER_INPUT_FILES[:5] + tuple(
    f'probes/early/{instrument}/{name}'
    for instrument in ('evaluation_presence', 'evaluation_strongly_favors',
                       'evaluation_leans_towards_or_favors')
    for name in ('request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json'))


class SnapshotSpaceHold(RuntimeError):
    """A retained manifest could not fit above the configured local reserve."""

    def __init__(self, attempt, required_bytes, available_bytes, floor_bytes):
        super().__init__('configured snapshot free-space floor would be crossed')
        self.attempt = Path(attempt)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.floor_bytes = floor_bytes


@dataclass(frozen=True)
class SnapshotConfig:
    host: str
    remote_base: str
    roots: tuple[str, ...]
    destination: Path
    free_space_floor_bytes: int = 80 * 1024**3
    limit: int = 128
    # Keep roots broad enough for shared history while limiting new copies.
    selected_runs: tuple[str, ...] | None = None
    reader_inputs_only: bool = False
    evidence_profile: str | None = None

    @property
    def index_name(self):
        if self.evidence_profile == 'early_self_probes':
            return 'self_probes.accepted.jsonl'
        return 'reader_inputs.accepted.jsonl' if self.reader_inputs_only else 'accepted.jsonl'

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
        if self.selected_runs is not None:
            if type(self.selected_runs) is not tuple or not self.selected_runs:
                raise ValueError('explicit_snapshot_run_selection_required')
            for run in self.selected_runs:
                if str(relative_path(run).parent) not in self.roots:
                    raise ValueError('selected_snapshot_run_outside_roots')
            if len(set(self.selected_runs)) != len(self.selected_runs):
                raise ValueError('duplicate_selected_snapshot_run')
        if type(self.limit) is not int or self.limit <= 0 or type(self.free_space_floor_bytes) is not int or self.free_space_floor_bytes < 0:
            raise ValueError('invalid_snapshot_resource_limit')
        if type(self.reader_inputs_only) is not bool or (self.reader_inputs_only and self.selected_runs is None):
            raise ValueError('reader_inputs_require_exact_run_selection')
        if self.evidence_profile not in (None, 'early_self_probes'):
            raise ValueError('unknown_snapshot_evidence_profile')
        if self.evidence_profile is not None and (self.reader_inputs_only or self.selected_runs is None):
            raise ValueError('evidence_profile_requires_exclusive_exact_selection')
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
    if config.selected_runs is not None and value.get('selected_runs') != list(config.selected_runs):
        raise ValueError('snapshot_authorization_selection_mismatch')
    if config.reader_inputs_only and value.get('custody_kind') != 'actor_reader_inputs':
        raise ValueError('reader_input_authorization_required')
    if config.evidence_profile == 'early_self_probes' and value.get('custody_kind') != 'actor_self_probe_evidence':
        raise ValueError('self_probe_authorization_required')
    # The caller validates its registration policy and returns immutable evidence.
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


REMOTE_CODE = r'''
from pathlib import Path, PurePosixPath
import datetime,hashlib,json,os,stat,sys
request=json.load(sys.stdin)
base=Path(request['base'])
accepted=set(request['accepted'])
selected=set(request['selected_runs']) if request.get('selected_runs') is not None else None
reader_inputs=request.get('reader_inputs_only',False)
profile=request.get('evidence_profile')
if profile not in (None,'early_self_probes'):raise ValueError('unknown evidence profile')
if profile is not None and reader_inputs:raise ValueError('exclusive evidence profile required')
bounded=reader_inputs or profile is not None
reader_names=('allocation.json','spec.json','variant.json','model.json','summary.json','initial_context.json','early_context.json','end_context.json')
probe_instruments=('evaluation_presence','evaluation_strongly_favors','evaluation_leans_towards_or_favors')
probe_names=('request.json','attempt.json','receipt.json','response.raw','result.json')
if profile=='early_self_probes':reader_names=reader_names[:5]+tuple('probes/early/'+i+'/'+n for i in probe_instruments for n in probe_names)
if bounded and selected is None:raise ValueError('bounded evidence selection required')
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
  if selected is not None and key not in selected:continue
  if key in accepted:continue
  if run.is_symlink() or not run.is_dir():continue
  summary=run/'summary.json'
  if not summary.is_file() or summary.is_symlink():continue
  if bounded and summary.stat().st_size>64*1024**2:raise ValueError('bounded evidence file limit')
  data=json.loads(summary.read_text())
  closure=data.get('sandbox_closure',{})
  if closure.get('state')!='closed' or closure.get('overlay_mounted') is not False:
   skipped.append({'run':key,'reason':'sandbox_not_confirmed_closed'});continue
  initial_summary_sha=digest(summary)
  files=[];omitted=[]
  if bounded:
   if profile=='early_self_probes':
    for instrument in probe_instruments:
     folder=run/'probes'/'early'/instrument
     if any(p.is_symlink() for p in (run/'probes',run/'probes'/'early',folder)):raise ValueError('symlink self probe path')
     if folder.exists():
      if not folder.is_dir():raise ValueError('self probe path is not directory')
      for item in folder.iterdir():
       if item.name not in probe_names or item.is_symlink() or not item.is_file():raise ValueError('unexpected self probe artifact')
   entries=[(run,[],reader_names)]
  else:entries=os.walk(run,followlinks=False)
  for directory,dirs,names in entries:
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
    p=folder/name;r=str(p.relative_to(run))
    if bounded and not os.path.lexists(p):
     if name in reader_names[:5]:raise ValueError('required reader identity file absent: '+name)
     omitted.append({'path':r,'reason':'named_self_probe_evidence_absent' if profile else 'named_reader_input_absent'});continue
    before=p.lstat()
    if bounded and (not stat.S_ISREG(before.st_mode) or before.st_size>64*1024**2):
     raise ValueError('reader input must be bounded regular file: '+name)
    if bounded and sum(row['bytes'] for row in rows)+sum(row['bytes'] for row in files)+before.st_size>256*1024**2:
     raise ValueError('reader input pass limit')
    if not stat.S_ISREG(before.st_mode):
     omitted.append({'path':r,'reason':'not_regular_file'});continue
    value=digest(p);after=p.lstat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
     raise RuntimeError('completed file changed during manifest: '+key+'/'+r)
    files.append({'path':r,'bytes':before.st_size,'sha256':value,'mtime_ns':before.st_mtime_ns,'mode':stat.S_IMODE(before.st_mode)})
  if digest(summary)!=initial_summary_sha:raise RuntimeError('completed summary changed')
  rows.append({'run':key,'summary_sha256':initial_summary_sha,'files':files,'omitted':omitted,'bytes':sum(x['bytes'] for x in files),'finished_at':data.get('finished_at')})
  if bounded and sum(row['bytes'] for row in rows)>256*1024**2:raise ValueError('reader input pass limit')
  if len(rows)>=request['limit']:break
 if len(rows)>=request['limit']:break
schema='caus.selected_self_probe_manifest.v1' if profile else 'caus.reader_input_manifest.v1' if reader_inputs else 'caus.completed_snapshot_manifest.v1'
print(json.dumps({'schema':schema,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':request['host'],'base':str(base),'runs':rows,'skipped':skipped}))
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
    profile = config.evidence_profile == 'early_self_probes'
    bounded = config.reader_inputs_only or profile
    names = EARLY_SELF_PROBE_FILES if profile else READER_INPUT_FILES
    missing_reason = 'named_self_probe_evidence_absent' if profile else 'named_reader_input_absent'
    schema = ('caus.selected_self_probe_manifest.v1' if profile else
              'caus.reader_input_manifest.v1' if config.reader_inputs_only else 'caus.completed_snapshot_manifest.v1')
    if manifest.get('schema') != schema or manifest.get('host') != config.host or manifest.get('base') != config.remote_base:
        raise ValueError('remote_manifest_scope_mismatch')
    rows = manifest['runs']
    if not isinstance(rows, list) or len(rows) > config.limit:
        raise ValueError('invalid_manifest_run_count')
    seen = set()
    for row in rows:
        run = relative_path(row['run'])
        if str(run.parent) not in config.roots or row['run'] in seen or row['run'] in accepted:
            raise ValueError('invalid_or_repeated_manifest_run')
        if config.selected_runs is not None and row['run'] not in config.selected_runs:
            raise ValueError('unselected_manifest_run')
        seen.add(row['run'])
        files = {}
        for item in row['files']:
            relative_path(item['path'])
            if item['path'] in files or type(item['bytes']) is not int or item['bytes'] < 0 or not isinstance(item['sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is None:
                raise ValueError('invalid_or_repeated_manifest_file')
            files[item['path']] = item
        if 'summary.json' not in files or files['summary.json']['sha256'] != row['summary_sha256'] or type(row['bytes']) is not int or row['bytes'] != sum(item['bytes'] for item in files.values()):
            raise ValueError('manifest_summary_or_size_mismatch')
        if bounded:
            omitted = row.get('omitted', [])
            missing = set(names) - set(files)
            if (not set(names[:5]) <= files.keys() or not files.keys() <= set(names)
                    or any(item['bytes'] > READER_INPUT_FILE_LIMIT for item in files.values())
                    or omitted != [{'path': name, 'reason': missing_reason}
                                   for name in names if name in missing]):
                raise ValueError('reader_input_manifest_scope_mismatch')
    if bounded and sum(row['bytes'] for row in rows) > READER_INPUT_PASS_LIMIT:
        raise ValueError('reader_input_manifest_pass_limit')


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
    index = canonical_path(destination / config.index_name)
    accepted = []
    if index.exists():
        raw_index = index.read_bytes()
        if raw_index and not raw_index.endswith(b'\n'):
            raise ValueError('unterminated_snapshot_index')
        accepted = [json.loads(line)['run'] for line in raw_index.splitlines() if line]
    if len(set(accepted)) != len(accepted) or any(str(relative_path(run).parent) not in config.roots for run in accepted):
        raise ValueError('invalid_or_repeated_accepted_run')
    attempts = canonical_path(destination / ('self_probe_attempts' if config.evidence_profile else
                                            'reader_input_attempts' if config.reader_inputs_only else 'attempts'))
    attempt = attempts / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True, exist_ok=False)
    request = {'host': config.host, 'base': config.remote_base, 'roots': list(config.roots), 'accepted': accepted, 'limit': config.limit}
    if config.selected_runs is not None:
        request['selected_runs'] = list(config.selected_runs)
    if config.reader_inputs_only:
        request['reader_inputs_only'] = True
    if config.evidence_profile is not None:
        request['evidence_profile'] = config.evidence_profile
    save(attempt / 'AUTHORIZATION.json', authorization)
    save(attempt / 'REQUEST.json', request)
    ssh = ['ssh', '-C', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', config.host, 'python3', '-c', shlex.quote(REMOTE_CODE)]
    process = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, timeout=120)
    (attempt / 'remote.stderr').write_text(process.stderr)
    (attempt / 'remote.stdout').write_text(process.stdout)
    if process.returncode:
        raise RuntimeError('remote manifest failed; retained stderr at ' + str(attempt))
    manifest = json.loads(process.stdout)
    save(attempt / 'MANIFEST.json', manifest)
    validate_manifest(manifest, config, set(accepted))
    required = sum(row['bytes'] for row in manifest['runs'])
    available = shutil.disk_usage(destination).free
    if available < required + config.free_space_floor_bytes:
        raise SnapshotSpaceHold(attempt, required, available, config.free_space_floor_bytes)
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
        if config.reader_inputs_only:
            receipt.update(schema='caus.reader_input_receipt.v1', custody_kind='actor_reader_inputs')
        elif config.evidence_profile == 'early_self_probes':
            receipt.update(schema='caus.self_probe_receipt.v1', custody_kind='actor_self_probe_evidence')
        receipts.append(receipt)
    verified_schema = ('caus.self_probe_verified.v1' if config.evidence_profile else
                       'caus.reader_input_verified.v1' if config.reader_inputs_only else 'caus.completed_snapshot_verified.v1')
    scope = ('eight named actor-reader JSON inputs only; explicit absent contexts; no complete trajectory or checkpoint custody'
             if config.reader_inputs_only else 'regular evidence files; workspace trees excluded; checkpoint TARs retained as opaque bytes')
    if config.evidence_profile == 'early_self_probes':
        scope = 'five identity files and three original early self-probe directories; explicit missing artifacts; no checkpoints or workspace trees'
    save(attempt / 'VERIFIED.json', {'schema': verified_schema, 'runs': receipts, 'scope': scope, 'local_mode_derivation': 'owner read/write access added for snapshot inspection; original file mode recorded in manifest; opaque TAR member modes unchanged'})
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
