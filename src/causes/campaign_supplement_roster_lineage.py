"""Authenticate one explicit immutable-roster source transition from saved bytes.

This is not a suffix assembler or execution authority. The caller must supply
the unchanged, reviewed baseline validator and bind that implementation to the
``parent_validator_source`` pin. It validates the literal baseline first; this
module never feeds a source projection to that validator. Its independently
accepted baseline must already contain the genuine closure, ordered suffix,
failure counters, budgets and ownership checks required by that validator.

Only three operational source roles, the explicit proof option, its lineage
record and an append-only evidence roster can change. Native queues, actors,
constructors and every other plan field remain exact. Large roster inputs are
compared as pins, not read here; the eventual proof must fully authenticate its
allowlist initially and finally. The caller's pinned reader owns nofollow,
stable-identity checks, hard resource limits and final input revalidation.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re


TRANSITION = 'causes.immutable_roster_source_transition.v1'
OPERATIONAL_ROLES = frozenset(('driver', 'owned_suffix', 'source_lineage'))
PIN_KEYS = frozenset(('path', 'bytes', 'sha256'))


class RosterLineageError(ValueError):
    """An exact reviewed operational transition could not be established."""


def _require(value, reason):
    if not value:
        raise RosterLineageError(reason)


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise RosterLineageError('invalid_json_value') from error


def _same(left, right):
    # Python's True == 1 and 1 == 1.0 must not weaken counter/flag equality.
    return _encoded(left) == _encoded(right)


def _pin(ref, limit=128 * 1024**2):
    _require(type(ref) is dict and set(ref) == PIN_KEYS, 'pin_shape')
    path, size, sha = ref['path'], ref['bytes'], ref['sha256']
    _require(type(path) is str and len(path.encode('utf-8')) <= 4096
             and path.startswith('/') and not path.startswith('//')
             and '\x00' not in path and str(PurePosixPath(path)) == path
             and '..' not in PurePosixPath(path).parts
             and type(size) is int and 0 <= size <= limit
             and type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha),
             'pin_shape_or_bound')


def _references(value, depth=0):
    _require(depth <= 64, 'metadata_depth_bound')
    if type(value) is dict:
        if set(value) == PIN_KEYS:
            _pin(value)
            yield value
        else:
            for child in value.values():
                yield from _references(child, depth + 1)
    elif type(value) is list:
        for child in value:
            yield from _references(child, depth + 1)


class _Reader:
    def __init__(self, read):
        _require(callable(read), 'reader_required')
        self.read = read
        self.refs = {}
        self.calls = self.requested = 0

    def raw(self, ref):
        _pin(ref, 8 * 1024**2)
        path = ref['path']
        _require(path not in self.refs or _same(self.refs[path], ref), 'conflicting_pin')
        self.calls += 1
        self.requested += ref['bytes']
        _require(self.calls <= 4096 and self.requested <= 64 * 1024**2,
                 'metadata_read_bound')
        body = self.read(dict(ref))
        _require(type(body) is bytes and len(body) == ref['bytes']
                 and hashlib.sha256(body).hexdigest() == ref['sha256'], 'pin_bytes')
        self.refs[path] = dict(ref)
        return body

    def value(self, ref):
        def unique(pairs):
            result = {}
            for key, item in pairs:
                _require(key not in result, 'duplicate_json_key')
                result[key] = item
            return result

        def nonfinite(_):
            raise RosterLineageError('nonfinite_json')

        try:
            value = json.loads(self.raw(ref), object_pairs_hook=unique,
                               parse_constant=nonfinite)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise RosterLineageError('invalid_json') from error
        _require(type(value) is dict, 'metadata_object')
        list(_references(value))  # Bound nesting even when no pins are present.
        return value


def validate_transition(reference, *, plan, read, validate_parent):
    """Return an exact source proof, never permission to run the candidate.

    ``validate_parent(baseline, read=...)`` is a trusted, separately pinned code
    dependency, not a boolean permission callback. It must run the original
    full lineage/suffix validation on the literal baseline and return its
    ``required_pins``. This pure module cannot identify a Python callable by a
    source pin; the caller's reviewed binding must establish that association.
    A callback that skips the original validation violates this API contract.

    The baseline source peer and adoption for this transition bind the same
    baseline and validator. No arbitrary list of permissible field changes or
    source roles is accepted. A separate future transition is needed to change
    those rules or to continue from a plan already using proof reuse.
    """
    _require(callable(validate_parent), 'parent_validator_required')
    reader = _Reader(read)
    try:
        transition = reader.value(reference)
        keys = {'schema', 'runtime_authorized', 'baseline_plan', 'baseline_review',
                'parent_validator_source', 'sources', 'source_preparation',
                'source_derivation', 'source_review', 'root_source_adoption',
                'policy', 'helper_source', 'binder_source', 'builder_source'}
        _require(set(transition) == keys and transition['schema'] == TRANSITION
                 and transition['runtime_authorized'] is False, 'explicit_transition')
        baseline = reader.value(transition['baseline_plan'])
        reader.raw(transition['parent_validator_source'])
        _require(baseline.get('immutable_roster_reuse') is None, 'baseline_already_reuses')
        before = _encoded(baseline)
        historical = validate_parent(baseline, read=reader.raw)
        _require(_encoded(baseline) == before, 'parent_validator_changed_baseline')
        _require(type(historical) is dict and type(historical.get('required_pins')) is list,
                 'parent_validation_result')
        for ref in historical['required_pins']:
            reader.raw(ref)

        # Parent validation above precedes all acceptance of the new sources.
        baseline_peer = reader.value(transition['baseline_review'])
        _require(baseline_peer.get('accepted_original_owned_suffix_baseline') is True
                 and baseline_peer.get('runtime_authorized') is False
                 and baseline_peer.get('findings') == []
                 and _same(baseline_peer['plan'], transition['baseline_plan'])
                 and _same(baseline_peer['validator_source'], transition['parent_validator_source']),
                 'baseline_acceptance')
        descriptor = baseline['predecessor_custody']
        _require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                 'closed_descriptor')
        actual, closed_peer = reader.value(descriptor['actual']), reader.value(descriptor['review'])
        _require(closed_peer.get('accepted_finite_supplement_closed_custody') is True
                 and closed_peer.get('accepted_completed_actor_saved_closure') is True
                 and closed_peer.get('findings') == []
                 and _same(closed_peer['actual'], descriptor['actual'])
                 and _same(closed_peer['source'], actual['source'])
                 and _same(closed_peer['qualification_findings'], actual['qualification_findings'])
                 and all(obj.get(key) is True for obj in (actual, closed_peer)
                         for key in ('all_original_attempts_retained', 'original_denominator_preserved')),
                 'closed_peer')
        _require(actual['native_invocations']
                 and _same(actual['native_invocations'][-1]['plan'], actual['plan'])
                 and type(actual['active']) is int and actual['active'] == 0
                 and actual.get('clean_native_ending') is True and actual['ambiguous'] == []
                 and actual['unresolved_claimed_ids'] == [] and actual['quarantined'] is False,
                 'closed_owner')

        old_sources, sources = baseline['sources'], transition['sources']
        _require(type(old_sources) is dict and type(sources) is dict
                 and set(sources) == set(old_sources)
                 and OPERATIONAL_ROLES | {'native_queue', 'completed_actor'} <= set(sources)
                 and {key for key in sources if not _same(sources[key], old_sources[key])}
                 == OPERATIONAL_ROLES, 'operational_source_delta')
        for ref in list(old_sources.values()) + list(sources.values()):
            reader.raw(ref)
        for key in ('helper_source', 'binder_source', 'builder_source'):
            reader.raw(transition[key])
        prep = reader.value(transition['source_preparation'])
        derivation = reader.value(transition['source_derivation'])
        peer = reader.value(transition['source_review'])
        adoption = reader.value(transition['root_source_adoption'])
        policy = reader.value(transition['policy'])
        shared = {'baseline_plan', 'baseline_review', 'parent_validator_source',
                  'sources', 'helper_source', 'binder_source', 'builder_source',
                  'source_derivation', 'policy'}
        _require(all(_same(obj[key], transition[key]) for obj in (prep, peer, adoption) for key in shared)
                 and all(_same(obj['parent_sources'], old_sources) for obj in (prep, peer, adoption))
                 and all(_same(obj['source_preparation'], transition['source_preparation'])
                         for obj in (peer, adoption))
                 and all(obj.get('runtime_authorized') is False for obj in (prep, peer, adoption))
                 and peer.get('accepted_immutable_roster_reuse_source') is True
                 and adoption.get('accepted_immutable_roster_reuse_source') is True
                 and adoption.get('root_authorized') is True
                 and peer.get('findings') == adoption.get('findings') == []
                 and adoption['schema'] == 'causes.immutable_roster_reuse_root_adoption.v1'
                 and _same(adoption['source_review'], transition['source_review'])
                 and all(_same(obj['source'], transition['helper_source'])
                         and _same(obj['driver'], sources['driver']) for obj in (peer, adoption)),
                 'transition_acceptance')
        _require(derivation.get('only_immutable_roster_proof_reuse_changed') is True
                 and derivation.get('unchanged_scientific_inputs_and_limits') is True
                 and derivation.get('default_opt_in') is False
                 and _same(derivation['sources'], sources)
                 and _same(derivation['parent_sources'], old_sources)
                 and all(_same(derivation[key], transition[key]) for key in
                         ('helper_source', 'binder_source', 'builder_source', 'baseline_plan')),
                 'transition_derivation')

        _require(set(policy) == {'schema', 'helper', 'allowlist', 'roster_sha256'}
                 and policy['schema'] == 'causes.immutable_roster_reuse_policy.v1'
                 and _same(policy['helper'], transition['helper_source']), 'exact_policy')
        roster = baseline['execution_pins']
        _require(type(roster) is list and len(roster) <= 4096, 'baseline_roster_bound')
        by_path = {}
        for ref in roster:
            _pin(ref)
            _require(ref['path'] not in by_path or _same(ref, by_path[ref['path']]),
                     'baseline_roster_conflict')
            by_path[ref['path']] = ref
        allowlist = policy['allowlist']
        _require(type(allowlist) is list and 1 <= len(allowlist) <= 1024,
                 'allowlist_bound')
        protected = list(_references({k: v for k, v in baseline.items() if k != 'execution_pins'}))
        protected += list(_references(reader.value(baseline['constructor'])))
        protected += list(sources.values()) + list(reader.refs.values())
        protected_paths = {ref['path'] for ref in protected}
        output = PurePosixPath(baseline['output_root'])
        _require(str(output) == baseline['output_root'] and output.is_absolute()
                 and not str(output).startswith('//') and '\x00' not in str(output)
                 and '..' not in output.parts, 'output_root')
        seen = set()
        for ref in allowlist:
            _pin(ref)
            path = ref['path']
            _require(path not in seen and path in by_path and _same(ref, by_path[path])
                     and path not in protected_paths and not path.endswith('.py')
                     and not PurePosixPath(path).is_relative_to(output), 'allowlist_unprotected_input')
            seen.add(path)
        _require(sum(ref['bytes'] for ref in allowlist) <= 512 * 1024**2
                 and len(_encoded(allowlist)) <= 1024**2
                 and hashlib.sha256(_encoded(allowlist)).hexdigest() == policy['roster_sha256'],
                 'allowlist_digest_or_bound')
        for path, ref in reader.refs.items():
            _require(path not in by_path or _same(ref, by_path[path]), 'execution_pin_conflict')
        additions = [reader.refs[path] for path in sorted(reader.refs) if path not in by_path]
        expected = dict(baseline, sources=sources, native_source_migration=reference,
                        immutable_roster_reuse={'enabled': True, 'policy': transition['policy'],
                            'source_review': transition['source_review'],
                            'root_adoption': transition['root_source_adoption']},
                        execution_pins=roster + additions)
        _require(type(plan) is dict and _same(plan, expected), 'candidate_differs_from_exact_projection')
        return {'kind': 'authenticated_immutable_roster_source_transition',
                'baseline_plan': dict(transition['baseline_plan']),
                'parent_validation': historical,
                'required_pins': [reader.refs[path] for path in sorted(reader.refs)],
                'allowlist_sha256': policy['roster_sha256'],
                'read_calls': reader.calls, 'requested_bytes': reader.requested,
                'runtime_authorized': False}
    except (KeyError, TypeError, IndexError, UnicodeError, RecursionError) as error:
        raise RosterLineageError('transition_shape') from error
