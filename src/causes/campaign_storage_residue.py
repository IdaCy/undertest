import json
from pathlib import Path
import subprocess

from causes import campaign_handoff as h
from causes import campaign_storage as storage


def command(argv, timeout):
    before = h.now()
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    storage.require(len(result.stdout) <= 2 * 1024**2 and len(result.stderr) <= 65536,
                    'bounded_mount_metadata_required')
    value = {'argv': argv, 'started_at': before, 'ended_at': h.now(),
             'returncode': result.returncode, 'stdout': result.stdout.decode(),
             'stderr': result.stderr.decode()}
    storage.require(result.returncode == 0, 'mount_preflight_metadata_failed')
    return value


def no_prior_writers(retired_roots, *, campaign_label, command_runner=command, mountinfo=None):
    storage.require(type(retired_roots) in (list, tuple) and retired_roots,
                    'explicit_retired_roots_required')
    roots = [h.canonical_path(root) for root in retired_roots]
    storage.require(len(roots) == len(set(roots)), 'distinct_retired_roots_required')
    storage.require(type(campaign_label) is str and campaign_label
                    and not any(char in campaign_label for char in ('\x00', '\n', '\r')),
                    'explicit_campaign_label_required')
    raw = Path('/proc/self/mountinfo').read_text() if mountinfo is None else mountinfo
    rows = storage.mount_rows(raw)
    storage.require(not any(Path(row['target']).is_relative_to(root) for row in rows for root in roots),
                    'residual_child_mount')
    listed = command_runner(['docker', 'ps', '--filter', 'label=' + campaign_label,
                             '--format', '{{.ID}}'], 15)
    ids = listed['stdout'].splitlines()
    storage.require(len(ids) <= 64 and len(ids) == len(set(ids))
                    and all(value and set(value) <= set('0123456789abcdef') for value in ids),
                    'bounded_campaign_container_ids_required')
    inspected = []
    for container in ids:
        record = command_runner(['docker', 'inspect', '--format', '{{json .Mounts}}', container], 15)
        mounts = json.loads(record['stdout'])
        storage.require(type(mounts) is list and all(type(row) is dict for row in mounts),
                        'container_mount_metadata_required')
        storage.require(not any(Path(row['Source']).is_relative_to(root)
                                for row in mounts if row.get('Source') for root in roots),
                        'residual_container_binding')
        inspected.append(record)
    return {'mount_rows': rows, 'containers': listed, 'container_mounts': inspected,
            'scope': {'retired_roots': [str(root) for root in roots],
                      'campaign_label': campaign_label}}
