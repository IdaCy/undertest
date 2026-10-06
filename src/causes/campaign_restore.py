from __future__ import annotations

import argparse
import base64
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import time

from causes import campaign_sandbox as sandbox


SANDBOX_SHA256 = '67a77f7d2ef8f3d9558d97adebd8c8c07ad58e8663a70acfb35b529533a1f3f5'
OVERLAY_XATTRS = {'trusted.overlay.origin', 'trusted.overlay.uuid',
                 'trusted.overlay.impure', 'trusted.overlay.opaque'}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            value.update(chunk)
    return value.hexdigest()


def regular_file(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
        raise ValueError('restore_input_path_not_canonical')
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError('restore_input_not_regular_file')
    return path


def read_pin(pin):
    if set(pin) != {'path', 'bytes', 'sha256'} or type(pin['bytes']) is not int:
        raise ValueError('restore_input_pin_invalid')
    path = regular_file(pin['path'])
    if path.stat().st_size != pin['bytes'] or digest_file(path) != pin['sha256']:
        raise ValueError('restore_input_hash_mismatch:' + path.name)
    return path


def file_pin(path):
    path = regular_file(path)
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest_file(path)}


def save_new(path, value):
    with Path(path).open('xb') as stream:
        stream.write(encoded(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def attributes(path):
    return {key: base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode()
            for key in sorted(os.listxattr(path, follow_symlinks=False))}


def tree_manifest(root):
    """Pin an existing lower without following symlinks or changing its index."""
    root = sandbox._real_directory(root)
    entries = []
    paths = [root]
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        paths.extend(parent / name for name in sorted(dirs + files))
        dirs[:] = sorted(name for name in dirs if not (parent / name).is_symlink())
    for path in sorted(paths):
        before = path.lstat()
        item = {'path': str(path.relative_to(root)), 'device': before.st_dev,
                'inode': before.st_ino, 'mode': stat.S_IMODE(before.st_mode),
                'uid': before.st_uid, 'gid': before.st_gid, 'mtime_ns': before.st_mtime_ns,
                'xattrs': attributes(path)}
        if stat.S_ISDIR(before.st_mode):
            item['kind'] = 'directory'
        elif stat.S_ISREG(before.st_mode):
            item.update(kind='regular', bytes=before.st_size, sha256=digest_file(path))
        elif stat.S_ISLNK(before.st_mode):
            item.update(kind='symlink', target=os.readlink(path))
        else:
            raise ValueError('unsupported_lower_special_file')
        after = path.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('lower_changed_during_manifest')
        entries.append(item)
    return {'schema': 'caus.same_lower_manifest.v1', 'root': str(root), 'entries': entries}


def member_name(name):
    if name in ('.', './'):
        return '.'
    if name.startswith('./'):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or str(path) != name or any(part in ('.', '..') for part in path.parts) or '\0' in name:
        raise ValueError('unsafe_restore_archive_member')
    return name


def inspect_archive(path, role):
    """Validate the complete archive before privileged extraction."""
    records = {}
    with tarfile.open(path, mode='r:') as archive:
        for member in archive:
            name = member_name(member.name)
            if name in records or member.mode & 0o6000:
                raise ValueError('duplicate_or_setid_restore_archive_member')
            if name == '.' and not member.isdir():
                raise ValueError('archive_root_not_directory')
            item = {'name': name, 'uid': member.uid, 'gid': member.gid, 'mode': member.mode,
                    'mtime_ns': int(Decimal(member.pax_headers.get('mtime', str(member.mtime))) * 10**9),
                    'xattrs': {}}
            for key, value in member.pax_headers.items():
                if key.startswith('SCHILY.acl.'):
                    raise ValueError('acl_restore_requires_separate_qualification')
                if key.startswith('SCHILY.xattr.'):
                    attribute = key.removeprefix('SCHILY.xattr.')
                    if not attribute.startswith('user.') and attribute not in OVERLAY_XATTRS:
                        raise ValueError('unsupported_restore_xattr:' + attribute)
                    if role != 'workspace_upper' and attribute in OVERLAY_XATTRS:
                        raise ValueError('overlay_attribute_outside_upper')
                    item['xattrs'][attribute] = base64.b64encode(value.encode('utf-8', 'surrogateescape')).decode()
            if member.isdir():
                item['kind'] = 'directory'
            elif member.isfile():
                stream = archive.extractfile(member)
                value = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1048576), b''):
                    value.update(chunk)
                item.update(kind='regular', bytes=member.size, sha256=value.hexdigest())
            elif member.issym():
                if '\0' in member.linkname:
                    raise ValueError('invalid_symlink_target')
                item.update(kind='symlink', target=member.linkname)
            elif member.islnk():
                target = member_name(member.linkname)
                if target not in records or records[target]['kind'] != 'regular':
                    raise ValueError('unsupported_forward_or_chained_hardlink')
                for key in ['uid', 'gid', 'mode', 'mtime_ns']:
                    if item[key] != records[target][key]:
                        raise ValueError('hardlink_metadata_disagreement')
                item['xattrs'] = copy.deepcopy(records[target]['xattrs'])
                item.update(kind='hardlink', target=target)
            elif member.ischr() and member.devmajor == member.devminor == 0 and role == 'workspace_upper':
                item['kind'] = 'whiteout'
            else:
                raise ValueError('unsupported_restore_special_file')
            records[name] = item
    if '.' not in records:
        raise ValueError('archive_root_metadata_missing')
    for name in records:
        if name == '.':
            continue
        for parent in PurePosixPath(name).parents:
            if str(parent) in records and records[str(parent)]['kind'] != 'directory':
                raise ValueError('archive_member_under_nondirectory')
            if str(parent) not in records:
                raise ValueError('archive_parent_directory_missing')
    return records


def verify_extracted(root, records):
    root = Path(root)
    found = {'.'}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        found.update(str((parent / name).relative_to(root)) for name in dirs + files)
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink()]
    if found != set(records):
        raise ValueError('restored_archive_membership_mismatch')
    for name, expected in records.items():
        path = root if name == '.' else root / name
        observed = path.lstat()
        kind = expected['kind']
        valid_type = {'directory': stat.S_ISDIR, 'regular': stat.S_ISREG,
                      'symlink': stat.S_ISLNK, 'hardlink': stat.S_ISREG,
                      'whiteout': stat.S_ISCHR}[kind](observed.st_mode)
        if not valid_type or (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode), observed.st_mtime_ns) != (expected['uid'], expected['gid'], expected['mode'], expected['mtime_ns']):
            raise ValueError('restored_member_metadata_mismatch:' + name)
        if attributes(path) != expected['xattrs']:
            raise ValueError('restored_member_xattrs_mismatch:' + name)
        if kind == 'regular' and (observed.st_size != expected['bytes'] or digest_file(path) != expected['sha256']):
            raise ValueError('restored_member_content_mismatch:' + name)
        if kind == 'symlink' and os.readlink(path) != expected['target']:
            raise ValueError('restored_symlink_mismatch:' + name)
        if kind == 'hardlink' and not os.path.samefile(path, root / expected['target']):
            raise ValueError('restored_hardlink_mismatch:' + name)
        if kind == 'whiteout' and os.major(observed.st_rdev) + os.minor(observed.st_rdev) != 0:
            raise ValueError('restored_whiteout_mismatch:' + name)


class RestoredSandbox(sandbox.Sandbox):
    """Restore filesystem state on the retained lower; never restore processes."""

    def __init__(self, plan_path, plan_sha256, output_dir, run_id):
        plan_path = regular_file(plan_path)
        if digest_file(plan_path) != plan_sha256 or digest_file(sandbox.__file__) != SANDBOX_SHA256:
            raise ValueError('restoration_plan_or_sandbox_source_mismatch')
        plan = json.loads(plan_path.read_bytes())
        if (plan.get('schema') != 'caus.filesystem_restore_plan.v1'
                or plan.get('process_state_restoration') is not False
                or plan.get('background_state_required') is not False):
            raise ValueError('unsupported_restore_plan')
        if plan['kernel_release'] != os.uname().release or plan['machine'] != os.uname().machine:
            raise ValueError('restore_host_compatibility_unqualified')
        inputs = {key: json.loads(read_pin(plan['inputs'][key]).read_bytes())
                  for key in ['spec', 'variant', 'checkpoint', 'state', 'lower_manifest']}
        spec, variant, checkpoint, state = (inputs[key] for key in ['spec', 'variant', 'checkpoint', 'state'])
        if checkpoint.get('schema') != 'caus.sandbox_checkpoint.v1' or checkpoint.get('complete') is not True or checkpoint.get('errors') or checkpoint.get('unpause_returncode') != 0:
            raise ValueError('incomplete_filesystem_capture')
        if checkpoint['workspace_method'] != 'overlayfs' or checkpoint['helper_sha256'] != SANDBOX_SHA256:
            raise ValueError('unsupported_checkpoint_implementation')
        if checkpoint['apparmor_profile_sha256'] != sandbox._APPARMOR_PROFILE_SHA256 or checkpoint['apparmor_profile'] != sandbox._APPARMOR_PROFILE:
            raise ValueError('checkpoint_sandbox_policy_mismatch')
        effective = copy.deepcopy(spec)
        overrides = dict(entry.split('=', 1) for entry in variant.get('env', []))
        effective['actor_env'] = [entry for entry in spec.get('actor_env', []) if entry.split('=', 1)[0] not in overrides]
        effective['actor_env'].extend(key + '=' + value for key, value in overrides.items())
        if hashlib.sha256(encoded(effective)).hexdigest() != checkpoint['seed_binding']['spec_sha256'] or checkpoint['seed_path'] != spec['seed_path']:
            raise ValueError('checkpoint_effective_spec_mismatch')
        if checkpoint['workspace'] != variant.get('workspace', spec['workspace']) or state['seed_binding'] != checkpoint['seed_binding'] or state['workspace'] != checkpoint['workspace']:
            raise ValueError('checkpoint_variant_or_state_binding_mismatch')
        if state['helper_sha256'] != SANDBOX_SHA256 or not state.get('image_id'):
            raise ValueError('source_sandbox_identity_incomplete')
        cue_records = [{'path': item['path'], 'mode': item.get('mode', 0o644),
                        'bytes': len(item['content'].encode()),
                        'sha256': hashlib.sha256(item['content'].encode()).hexdigest()}
                       for item in variant.get('files', [])]
        if state.get('cue_files', []) != cue_records:
            raise ValueError('captured_variant_cue_binding_mismatch')
        if inputs['lower_manifest'] != tree_manifest(spec['seed_path']):
            raise ValueError('exact_retained_lower_mismatch')
        expected_files = {str(Path(spec['task_root']) / 'rg')} if (Path(spec['task_root']) / 'rg').exists() else set()
        files = plan.get('readonly_files', {})
        if set(files) != expected_files:
            raise ValueError('readonly_tool_binding_set_mismatch')
        for path, pin in files.items():
            if str(read_pin(pin)) != path:
                raise ValueError('readonly_tool_path_mismatch')
        trees = plan.get('readonly_trees', {})
        if set(trees) != ({spec['module_path']} if spec.get('module_path') else set()):
            raise ValueError('readonly_module_binding_set_mismatch')
        for path, pin in trees.items():
            if json.loads(read_pin(pin).read_bytes()) != tree_manifest(path):
                raise ValueError('readonly_module_tree_mismatch')
        required = {'workspace_upper', 'tmpfs_tmp'} | ({'tmpfs_cache'} if spec.get('module_path') else set())
        captured = {row['role']: row for row in checkpoint['archives']}
        if len(captured) != len(checkpoint['archives']) or set(captured) != required or set(plan['archives']) != required:
            raise ValueError('checkpoint_archive_set_mismatch')
        self.restore_archives = {}
        for role in sorted(required):
            pin = plan['archives'][role]
            if any(pin[key] != captured[role][key] for key in ['bytes', 'sha256']):
                raise ValueError('checkpoint_archive_pin_mismatch')
            receipts = [row for row in state.get('operations', [])
                        if '-cf' in row.get('argv', [])
                        and row['argv'][row['argv'].index('-cf') + 1] == captured[role]['path']]
            if (len(receipts) != 1 or receipts[0].get('returncode') != 0
                    or receipts[0].get('timeout') is not False
                    or receipts[0].get('stderr_bytes') != 0):
                raise ValueError('capture_archive_warning_or_receipt_missing:' + role)
            archive = read_pin(pin)
            self.restore_archives[role] = (archive, inspect_archive(archive, role))
        self.restore_plan, self.restore_source_state = plan, state
        self.restore_admitted = False
        super().__init__(effective, output_dir, run_id, workspace_path=checkpoint['workspace'], files=variant.get('files', []))
        save_new(self.output / 'RESTORE_PLAN.json', plan)
        self.record['restoration'] = {'plan_sha256': plan_sha256, 'status': 'prepared',
                                     'process_state_restoration_supported': False,
                                     'same_retained_lower_required': True,
                                     'unsupported': ['process_memory', 'open_fds', 'unlinked_files', 'sockets', 'background_processes', '/dev/shm', 'ACLs', 'cross_host_or_rematerialized_lower']}
        self._save()

    def _extract(self, role, destination):
        path, members = self.restore_archives[role]
        read_pin(self.restore_plan['archives'][role])
        if list(Path(destination).iterdir()):
            raise ValueError('restoration_destination_not_empty')
        result = self._execute(['tar', '--extract', '--file', str(path), '--directory', str(destination),
                                '--xattrs', '--xattrs-include=*', '--acls', '--numeric-owner',
                                '--same-owner', '--same-permissions', '--delay-directory-restore'], 180, 'restore_' + role)
        if result['returncode'] or result['timeout'] or result['stderr_bytes']:
            raise RuntimeError('archive_restore_failed:' + role)
        verify_extracted(destination, members)
        self.record['restoration'].setdefault('verified_archives', []).append(role)
        self._save()

    def _workspace(self):
        if self.record['image_id'] != self.restore_source_state['image_id']:
            raise ValueError('restoration_image_identity_mismatch')
        expected_lower = json.loads(read_pin(self.restore_plan['inputs']['lower_manifest']).read_bytes())
        if expected_lower != tree_manifest(self.seed):
            raise ValueError('exact_retained_lower_changed_before_mount')
        for name in ['upper', 'work', 'merged']:
            (self.root / name).mkdir()
        self._extract('workspace_upper', self.root / 'upper')
        options = f"lowerdir={self.seed},upperdir={self.root / 'upper'},workdir={self.root / 'work'}"
        result = self._execute(['mount', '-t', 'overlay', 'overlay', '-o', options, str(self.repo)], 30, 'restore_mount')
        self.mounted = os.path.ismount(self.repo)
        if result['returncode'] or result['timeout'] or not self.mounted:
            raise RuntimeError('restored_overlay_mount_failed')
        self.record.update(workspace_method='overlayfs', workspace_host_path=str(self.repo))
        self._save()

    def _cue_files(self):
        original = self.files
        try:
            self.files = [item for item in original if item['path'].startswith('/')]
            super()._cue_files()
        finally:
            self.files = original
        self.record['cue_files'] = copy.deepcopy(self.restore_source_state.get('cue_files', []))
        self.record['restoration']['relative_cues_not_reapplied'] = True

    def start(self):
        if os.geteuid() != 0:
            raise RuntimeError('filesystem_restoration_requires_privileged_host_route')
        pause_attempted = False
        try:
            super().start()
            pause_attempted = True
            pause = self._execute(['docker', 'pause', self.name], 30, 'restore_pause')
            if pause['returncode'] or pause['timeout']:
                raise RuntimeError('restored_container_pause_failed')
            inspected = self._execute(['docker', 'inspect', '--format', '{{json .State}}', self.name], 30, 'restore_paused_state')
            state = json.loads(inspected['stdout'])
            if inspected['returncode'] or not state.get('Paused') or type(state.get('Pid')) is not int or state['Pid'] <= 0:
                raise RuntimeError('restored_container_identity_unavailable')
            for role in sorted(self.restore_archives):
                if role.startswith('tmpfs_'):
                    self._extract(role, Path('/proc') / str(state['Pid']) / 'root' / role.removeprefix('tmpfs_'))
            resumed = self._execute(['docker', 'unpause', self.name], 30, 'restore_unpause')
            if resumed['returncode'] or resumed['timeout']:
                raise RuntimeError('restored_container_unpause_failed')
            pause_attempted = False
            self.restore_admitted = True
            self.record['restoration'].update(status='filesystem_restored', finished_at=time.time())
            self._save()
            return copy.deepcopy(self.record)
        except BaseException:
            if pause_attempted:
                self._execute(['docker', 'unpause', self.name], 30, 'restore_failure_unpause')
            self.close()
            raise

    def run(self, command, timeout=90):
        if not self.restore_admitted:
            raise RuntimeError('restoration_not_admitted')
        return super().run(command, timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    box = RestoredSandbox(args.plan, args.plan_sha256, args.output, args.run_id)
    try:
        result = box.start()
        save_new(Path(args.output) / 'RESTORED.json', result)
    finally:
        closure = box.close()
        save_new(Path(args.output) / 'CLOSURE.json', closure)
    print(json.dumps({'status': result['restoration']['status'], 'closure': closure['state']}))


if __name__ == '__main__':
    main()
