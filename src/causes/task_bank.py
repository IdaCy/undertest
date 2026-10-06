import hashlib
import json
from pathlib import Path, PurePosixPath


ASSET_FIELDS = ('task_root', 'seed_path', 'module_path')


def _asset_directory(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('invalid_relative_asset_path')
    parts = relative.split('/')
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise ValueError('invalid_relative_asset_path')
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=True)
    if resolved != candidate or not resolved.is_relative_to(root):
        raise ValueError('asset_path_escape_or_symlink')
    if not candidate.is_dir():
        raise ValueError('asset_directory_required')
    return str(candidate)


def load_task(path, assets_root, *, expected_sha256=None):
    """Bind portable task paths to existing assets; this does not qualify or run them."""
    raw = Path(path).read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('portable_task_pin_mismatch')
    task = json.loads(raw)
    if task.get('schema') != 'causes.portable_task.v1':
        raise ValueError('portable_task_schema_mismatch')
    spec, assets = task.get('spec'), task.get('assets')
    if (not isinstance(spec, dict) or not isinstance(assets, dict)
            or not isinstance(task.get('task_id'), str) or not task['task_id']
            or spec.get('task_id') != task['task_id']):
        raise ValueError('portable_task_identity_mismatch')
    if set(assets) != set(ASSET_FIELDS) or any(field in spec for field in ASSET_FIELDS):
        raise ValueError('portable_task_asset_fields_mismatch')
    root = Path(assets_root).absolute()
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError('canonical_asset_directory_required')
    result = dict(spec)
    for field in ASSET_FIELDS:
        relative = assets[field]
        result[field] = None if field == 'module_path' and relative is None else _asset_directory(root, relative)
    return result
