"""Invoke one pinned native materializer with explicitly retained closure bytes."""

import base64
import copy
import json
from pathlib import Path
import sys
import types

from causes import campaign_handoff as h


SCHEMA = 'causes.materializer_custody_invocation.v1'
CUSTODY_SCHEMA = 'causes.materializer_closure_custody.v1'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def _key(ref):
    require(isinstance(ref, dict) and set(ref) == {'path', 'bytes', 'sha256'}, 'pin_fields')
    require(type(ref['bytes']) is int and ref['bytes'] >= 0, 'pin_size')
    require(isinstance(ref['sha256'], str) and len(ref['sha256']) == 64
            and all(c in '0123456789abcdef' for c in ref['sha256']), 'pin_digest')
    require(isinstance(ref['path'], str) and Path(ref['path']).is_absolute()
            and str(Path(ref['path'])) == ref['path'] and '..' not in Path(ref['path']).parts,
            'pin_path')
    return json.dumps(ref, sort_keys=True)


class ClosureReader:
    """Read only explicitly pinned copies; never create original-path aliases."""

    def __init__(self, custody_ref):
        self.custody_ref = copy.deepcopy(custody_ref)
        self.custody = h.checked(custody_ref)
        require(self.custody['schema'] == CUSTODY_SCHEMA, 'custody_schema')
        self._mapping = {}
        self._sources = {}
        for row in self.custody['aliases']:
            require(set(row) == {'source', 'local'}, 'custody_row_fields')
            source, local = row['source'], row['local']
            key = _key(source)
            _key(local)
            require((source['bytes'], source['sha256']) == (local['bytes'], local['sha256']),
                    'custody_content_mismatch')
            require(source['path'] not in self._sources, 'duplicate_custody_source')
            self._sources[source['path']] = copy.deepcopy(source)
            self._mapping[key] = copy.deepcopy(local)
        require(self._mapping, 'empty_closure_custody')
        self.verify()

    def raw(self, ref):
        key = _key(ref)
        require(key in self._mapping, 'undeclared_closure_reference')
        return h.checked_bytes(self._mapping[key])

    def __call__(self, ref):
        return json.loads(self.raw(ref))

    def verify(self):
        h.checked_bytes(self.custody_ref)
        for local in self._mapping.values():
            h.checked_bytes(local)


def _module_pins(expected):
    actual = {name: h.pin(module.__file__) for name, module in sys.modules.items()
              if (name == 'causes' or name.startswith('causes.'))
              and name != __name__ and getattr(module, '__file__', None)}
    require(all(name in expected and expected[name] == ref for name, ref in actual.items()),
            'materializer_import_changed')
    for ref in expected.values():
        h.checked_bytes(ref)
    return actual


def prepare(request_ref):
    """Check retained evidence and load the pinned core without materializing."""
    request = h.checked(request_ref)
    require(request['schema'] == SCHEMA and request['caller_source'] == h.pin(__file__),
            'invocation_source_or_schema')
    require(set(request) == {'schema', 'caller_source', 'materializer', 'inputs',
                            'closure_custody', 'module_sources', 'execution_directory'},
            'invocation_fields')
    inputs = h.checked(request['inputs'])
    require(inputs['delegated_adoption'] is None, 'adoption_must_be_supplied_separately')
    custody = ClosureReader(request['closure_custody'])
    require(custody.custody['closure'] == inputs['closed_predecessor']['closure'],
            'custody_predecessor_changed')
    _module_pins(request['module_sources'])
    source = h.checked_bytes(request['materializer'])
    native = types.ModuleType('pinned_native_materializer')
    native.__file__ = request['materializer']['path']
    exec(compile(source, native.__file__, 'exec'), native.__dict__)
    imports = _module_pins(request['module_sources'])
    require(callable(getattr(native, 'materialize', None)), 'native_materializer_missing')
    closed = native.succession.closed_predecessor(inputs['closed_predecessor'], reader=custody)
    # Native closure predicates validate identity, natural retirement and complete
    # pending membership. Also require retained bytes for the complete capture.
    for name in ('journal', 'history_proof', 'never_claimed', 'owned_pause', 'pause_ownership'):
        custody.raw(closed[name])
    proof = custody(closed['history_proof'])
    require(base64.b64decode(proof['journal_base64'], validate=True) == custody.raw(closed['journal']),
            'closure_journal_proof_changed')
    for ref in proof['block_pins']:
        custody.raw(ref)
    require(custody(closed['pause_ownership'])['marker'] == closed['owned_pause'],
            'closure_pause_marker_changed')
    custody.verify()
    return request, inputs, custody, native, imports


def run(request_ref, review_ref, adoption_ref):
    request = h.checked(request_ref)
    review = h.checked(review_ref)
    authority = h.checked(adoption_ref)
    own_source = h.pin(__file__)
    require(review.get('accepted_native_allocation_custody_caller') is True
            and review['request'] == request_ref and review['source'] == own_source
            and review['materializer'] == request['materializer'], 'caller_review_missing')
    require(authority.get('root_authorized') is True
            and authority['action'] == 'one_native_allocation_materialization'
            and authority['caller_request'] == request_ref
            and authority['caller_review'] == review_ref
            and authority['caller_source'] == own_source
            and authority['maximum_invocations'] == 1
            and authority['automatic_retry'] is False, 'caller_adoption_missing')
    request, inputs, custody, native, imports = prepare(request_ref)
    require(authority['inputs'] == inputs['binding']
            and authority['preparation_review'] == inputs['preparation_review']
            and authority['choice'] == inputs['choice'], 'native_adoption_binding_changed')
    native_output = h.canonical_path(inputs['output_directory'])
    outer_output = h.canonical_path(request['execution_directory'])
    require(not native_output.exists() and not native_output.is_symlink(), 'native_output_exists')
    require(native_output != outer_output
            and native_output not in outer_output.parents
            and outer_output not in native_output.parents, 'overlapping_outputs')
    output = h.new_record_directory(outer_output)
    intent = h.save_new(output, 'INTENT.json', dict(at=h.now(), request=request_ref,
        review=review_ref, adoption=adoption_ref, source=own_source,
        materializer=request['materializer'], imports=imports,
        maximum_invocations=1, automatic_retry=False))
    bound = copy.deepcopy(inputs)
    bound['delegated_adoption'] = adoption_ref
    native_calls, result, error = 0, None, None
    started = h.now()
    try:
        for ref in (request_ref, review_ref, adoption_ref, request['inputs'], request['materializer']):
            h.checked_bytes(ref)
        custody.verify()
        _module_pins(request['module_sources'])
        native_calls = 1
        result = native.materialize(bound, closure_reader=custody)
        h.checked_bytes(result)
        return result
    except BaseException as failure:
        error = dict(type=type(failure).__name__, message=str(failure)[:1000])
        raise
    finally:
        stability_error = None
        try:
            for ref in (own_source, request_ref, review_ref, adoption_ref,
                        request['inputs'], request['materializer']):
                h.checked_bytes(ref)
            custody.verify()
            _module_pins(request['module_sources'])
        except BaseException as failure:
            stability_error = dict(type=type(failure).__name__, message=str(failure)[:1000])
        h.save_new(output, 'EXECUTION.json', dict(intent=intent, started_at=started,
            finished_at=h.now(), request=request_ref, materializer=request['materializer'],
            native_invocations=native_calls, result=result, exception=error,
            stability_error=stability_error, completed=error is None and stability_error is None,
            automatic_retry=False))
        if stability_error is not None and error is None:
            raise ValueError('materializer_inputs_changed_after_invocation')


if __name__ == '__main__':
    require(len(sys.argv) == 4, 'expected_request_review_adoption_pins')
    print(json.dumps(run(*map(json.loads, sys.argv[1:]))))
