"""Read-only, finite-scope reconciliation of scientific request producers.

This checks explicit entrypoints and request/output paths, not arbitrary network
clients. Unrelated command lines are read only to match that finite scope and
are never returned. Service metrics remain a separate total-load constraint.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith('/') else []
    if isinstance(value, list):
        return [p for item in value for p in _paths(item)]
    if isinstance(value, dict):
        return [p for item in value.values() for p in _paths(item)]
    return []


def matches(argv: Sequence[str], scope: Mapping[str, Any]) -> list[str]:
    """Match argv tokens and JSON argument values; never search embedded code."""
    reasons: set[str] = set()
    sources = set(scope['entrypoints'])
    exact = set(scope['request_paths'])
    roots = scope['output_roots']
    for index, argument in enumerate(argv):
        if argument == '-m' and index + 1 < len(argv) and argv[index + 1] in scope['modules']:
            reasons.add('module')
        values = _paths(argument)
        if argument.startswith('--') and '=' in argument:
            values += _paths(argument.split('=', 1)[1])
        if argument.startswith(('{', '[')):
            try:
                values += _paths(json.loads(argument))
            except (ValueError, TypeError):
                pass
        for value in values:
            if value in sources:
                reasons.add('entrypoint')
            if value in exact:
                reasons.add('request_path')
            if any(value == root or value.startswith(root + '/') for root in roots):
                reasons.add('output_root')
    return sorted(reasons)


def validate_scope(scope: Mapping[str, Any]) -> None:
    require(set(scope) == {'host', 'entrypoints', 'descriptor_entrypoints', 'modules', 'request_paths', 'output_roots'}, 'census_scope_fields')
    require(isinstance(scope['host'], str) and bool(scope['host']), 'census_host')
    for key in ('entrypoints', 'descriptor_entrypoints', 'modules', 'request_paths', 'output_roots'):
        values = scope[key]
        require(isinstance(values, list) and values == sorted(set(values)), 'census_scope_order:' + key)
        require(all(isinstance(v, str) and v and '\0' not in v for v in values), 'census_scope_value:' + key)
    for key in ('entrypoints', 'descriptor_entrypoints', 'request_paths', 'output_roots'):
        require(all(v.startswith('/') and str(Path(v)) == v and v != '/' and '..' not in Path(v).parts for v in scope[key]), 'census_scope_path:' + key)
    require(bool(scope['entrypoints']) and bool(scope['output_roots']), 'census_scope_empty')


def descriptor_target(argv: Sequence[str], scope: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve a generic native loader without executing or trusting its target."""
    entries = [v for v in argv if v in scope['descriptor_entrypoints']]
    if not entries:
        return None
    require(len(entries) == 1, 'census_descriptor_entrypoint')
    at = argv.index(entries[0])
    require(len(argv) == at + 3 and argv[at + 1] == '--descriptor', 'census_descriptor_argv')

    def checked(ref: Mapping[str, Any], maximum: int) -> bytes:
        require(set(ref) == {'path', 'bytes', 'sha256'} and type(ref['bytes']) is int and 0 < ref['bytes'] <= maximum, 'census_descriptor_pin')
        path = Path(ref['path'])
        require(path.is_absolute() and path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)), 'census_descriptor_path')
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
        require(len(data) == ref['bytes'] and hashlib.sha256(data).hexdigest() == ref['sha256'], 'census_descriptor_bytes')
        return data

    ref = json.loads(argv[at + 2])
    value = json.loads(checked(ref, 64 * 1024))
    require(value.get('schema') == 'causes.pinned_native_source_entrypoint.v1' and value.get('strategy') == 'module', 'census_descriptor_schema')
    require(value['entrypoint']['path'] == entries[0], 'census_descriptor_entrypoint_pin')
    checked(value['entrypoint'], 1024 * 1024)
    checked(value['source'], 10 * 1024 * 1024)
    native = value['native_argv']
    require(isinstance(native, list) and all(isinstance(v, str) for v in native) and native.count(value['source']['path']) == 1, 'census_descriptor_native_argv')
    require(value['source']['path'] not in scope['descriptor_entrypoints'], 'census_nested_descriptor')
    return dict(descriptor=ref, source=value['source'], native_match_reasons=matches(native, scope))


def snapshot(scope: Mapping[str, Any], *, proc_root: Path = Path('/proc')) -> dict[str, Any]:
    """Collect stable PID/start/PPID identities without returning foreign argv."""
    validate_scope(scope)
    nodes: dict[int, dict[str, Any]] = {}
    matched: list[dict[str, Any]] = []
    resolved_nonproducers: list[dict[str, Any]] = []
    disappeared = 0
    for directory in sorted((p for p in proc_root.iterdir() if p.name.isdecimal()), key=lambda p: int(p.name)):
        try:
            before = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            raw = (directory / 'cmdline').read_bytes()
            after = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            disappeared += 1
            continue
        except PermissionError as error:
            raise ValueError('census_unreadable_pid:' + directory.name) from error
        require(before[19] == after[19] and before[1] == after[1], 'census_process_changed:' + directory.name)
        argv = [value.decode('utf-8', errors='surrogateescape') for value in raw.split(b'\0') if value]
        row = dict(pid=int(directory.name), process_starttime_ticks=after[19], ppid=int(after[1]), state=after[0], argv_sha256=hashlib.sha256(raw).hexdigest())
        nodes[row['pid']] = row
        reasons = matches(argv, scope)
        target = descriptor_target(argv, scope)
        if target:
            if target['native_match_reasons']:
                reasons.append('descriptor_target')
            else:
                resolved_nonproducers.append(dict(pid=row['pid'], process_starttime_ticks=row['process_starttime_ticks'], **target))
        if reasons:
            matched.append(dict(row, argv=argv, match_reasons=reasons))
    return dict(nodes=nodes, matched=matched, resolved_nonproducers=resolved_nonproducers, disappeared_during_read=disappeared)


def reconcile(scope: Mapping[str, Any], expected: Sequence[Mapping[str, Any]], observed: Mapping[str, Any]) -> dict[str, Any]:
    """Reject extra matching producers, including descendants of known owners."""
    validate_scope(scope)
    by_pid = {row['pid']: row for row in expected}
    require(len(by_pid) == len(expected), 'census_duplicate_expected_pid')
    live = [row for row in observed['matched'] if row['state'] not in ('Z', 'X', 'x')]
    require({row['pid'] for row in live} == set(by_pid), 'census_unexpected_or_missing_producer')
    for row in live:
        want = by_pid[row['pid']]
        require(set(want) == {'pid', 'process_starttime_ticks', 'argv'}, 'census_expected_identity_fields')
        require(all(row[key] == want[key] for key in want), 'census_producer_identity_changed')
    nodes = observed['nodes']
    relevant = set(by_pid)
    # Keep only hashes for unmatched ancestors/descendants, including legitimate
    # sandbox children. Being a descendant never exempts a matching producer.
    for pid in list(relevant):
        cursor = pid
        seen: set[int] = set()
        while cursor in nodes and cursor not in seen:
            seen.add(cursor)
            relevant.add(cursor)
            cursor = nodes[cursor]['ppid']
    descendants = set(by_pid)
    while True:
        enlarged = descendants | {pid for pid, row in nodes.items() if row['ppid'] in descendants}
        if enlarged == descendants:
            break
        descendants = enlarged
    relevant |= descendants
    return dict(host=scope['host'], scope=dict(scope), expected=list(expected), matched=observed['matched'], resolved_nonproducers=observed['resolved_nonproducers'], related_process_tree=[nodes[pid] for pid in sorted(relevant) if pid in nodes], disappeared_during_read=observed['disappeared_during_read'], complete_for_declared_scope=True, arbitrary_network_client_absence_established=False)


def census(scope: Mapping[str, Any], expected: Sequence[Mapping[str, Any]], *, proc_root: Path = Path('/proc')) -> dict[str, Any]:
    """Require two independently collected current matching-owner sets."""
    before = reconcile(scope, expected, snapshot(scope, proc_root=proc_root))
    after = reconcile(scope, expected, snapshot(scope, proc_root=proc_root))
    stable = lambda rows: [(r['pid'], r['process_starttime_ticks'], r['ppid'], r['argv']) for r in rows if r['state'] not in ('Z', 'X', 'x')]
    require(stable(before['matched']) == stable(after['matched']), 'census_owner_parent_changed')
    return dict(before=before, after=after, complete_for_declared_scope=True, arbitrary_network_client_absence_established=False)
