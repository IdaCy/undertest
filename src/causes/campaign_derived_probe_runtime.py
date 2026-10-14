"""Keep frozen derived-request identity separate from a reviewed queue change.

This adapter supplies request/worker callbacks only. It launches no queue and
provides no provider, ownership, storage, capacity, replay or runtime authority.
The caller must load and pin both module graphs and retain the ordinary guards.
"""
import ast
import copy
import hashlib
import json

from causes import campaign_supplement as core


def compatible_queue(scientific, operational, scientific_source, operational_source,
                     *, review=None, read=core.pinned):
    """Accept literal code or explicit reviewed operational queue changes.

Frozen protocol hashes continue to name the scientific module. The operational
module is not substituted into that protocol or used to rebuild input IDs.
"""
    def raw(ref):
        core.canonical(ref['path'])
        data = read(ref)
        if type(data) is not bytes or type(ref['bytes']) is not int or len(data) != ref['bytes'] or \
                hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise operational.IntegrityError('derived_runtime_source_pin')
        return data

    old, new = raw(scientific_source), raw(operational_source)
    for module, data in ((scientific, old), (operational, new)):
        if core.canonical(module.__file__).read_bytes() != data:
            raise operational.IntegrityError('derived_runtime_loaded_source')
    if old == new:
        if review is not None:
            raise operational.IntegrityError('derived_runtime_unnecessary_migration')
        return
    if review is None:
        raise operational.IntegrityError('derived_runtime_migration_required')
    peer = json.loads(raw(review))
    if peer.get('schema') == 'causes.derived_probe_healthy_queue_compatibility.v1':
        return _healthy_compatibility(peer, review, scientific_source,
                                      operational_source, old, new, raw,
                                      operational.IntegrityError)
    proof = peer['complete_inverse']
    if not (peer.get('accepted_admission_harvest_optimization_source') is True and
            peer.get('accepted_supplement_admission_harvest_source') is True and
            peer.get('source') == operational_source and peer.get('findings') == [] and
            proof['parent_bytes'] == len(old) and proof['parent_sha256'] == hashlib.sha256(old).hexdigest() and
            proof['added_bytes'] == len(new)-len(old) == 158 and
            proof['forward_exact'] is proof['reverse_exact'] is proof['all_other_bytes_literal'] is True and
            proof['changed_top_level_helpers'] == ['run_queue']):
        raise operational.IntegrityError('derived_runtime_unaccepted_migration')
    def helpers(data):
        tree = ast.parse(data)
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_queue']
        if len(found) != 1:
            raise operational.IntegrityError('derived_runtime_queue_shape')
        tree.body.remove(found[0])
        return ast.dump(tree, include_attributes=False)
    if helpers(old) != helpers(new):
        raise operational.IntegrityError('derived_runtime_request_helpers_changed')


def _healthy_compatibility(descriptor, reference, scientific_source,
                           operational_source, old, new, read, error):
    """Verify two accepted queue changes without replacing scientific identity.

    This proves compatibility of immutable code, not owner lineage or permission
    to enable the healthy-harvest option. The caller must separately validate its
    predecessor and bind that option in the plan, contract and queue invocation.
    Returned pins belong in the caller's ordinary entry/exit integrity roster.
    """
    refs = {r['path']: r for r in (reference, scientific_source, operational_source)}

    def require(value, reason):
        if not value:
            raise error(reason)

    def raw(ref):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'},
                'derived_healthy_pin')
        require(ref['path'] not in refs or refs[ref['path']] == ref,
                'derived_healthy_conflicting_pin')
        body = read(ref)
        refs[ref['path']] = ref
        return body

    def value(ref):
        result = json.loads(raw(ref))
        require(type(result) is dict, 'derived_healthy_object')
        return result

    def nested(item):
        if type(item) is dict:
            if set(item) == {'path', 'bytes', 'sha256'}:
                raw(item)
            else:
                for child in item.values():
                    nested(child)
        elif type(item) is list:
            for child in item:
                nested(child)

    def unchanged_helpers(body):
        tree = ast.parse(body)
        queues = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_queue']
        require(len(queues) == 1, 'derived_healthy_queue_shape')
        tree.body.remove(queues[0])
        return ast.dump(tree, include_attributes=False)

    try:
        require(set(descriptor) == {'schema', 'scientific_source', 'intermediate_source',
                'operational_source', 'completed_actor_source', 'early_review', 'healthy_review',
                'healthy_preparation', 'healthy_adoption', 'healthy_derivation'},
                'derived_healthy_descriptor')
        require(descriptor['scientific_source'] == scientific_source and
                descriptor['operational_source'] == operational_source,
                'derived_healthy_source_endpoints')
        intermediate_ref = descriptor['intermediate_source']
        intermediate = raw(intermediate_ref)
        early = value(descriptor['early_review']); proof = early['complete_inverse']
        require(early.get('accepted_admission_harvest_optimization_source') is True and
                early.get('accepted_supplement_admission_harvest_source') is True and
                early.get('source') == intermediate_ref and early.get('findings') == [] and
                proof['parent_bytes'] == len(old) and
                proof['parent_sha256'] == hashlib.sha256(old).hexdigest() and
                proof['added_bytes'] == len(intermediate)-len(old) == 158 and
                proof['forward_exact'] is proof['reverse_exact'] is proof['all_other_bytes_literal'] is True and
                proof['changed_top_level_helpers'] == ['run_queue'],
                'derived_healthy_first_transition')
        peer = value(descriptor['healthy_review'])
        preparation = value(descriptor['healthy_preparation'])
        adoption = value(descriptor['healthy_adoption'])
        derivation = value(descriptor['healthy_derivation'])
        selected = {'causes.campaign_supplement': operational_source,
                    'causes.campaign_supplement_completed_actor': descriptor['completed_actor_source']}
        parents = peer['parent_sources']
        require(type(derivation['proofs']) is list and len(derivation['proofs']) == 2 and
                {row['module'] for row in derivation['proofs']} == set(selected),
                'derived_healthy_derivation_roster')
        require(peer.get('accepted_healthy_harvest_continuation_source') is True and
                peer.get('accepted_completed_actor_healthy_harvest_source') is True and
                peer['sources'] == preparation['sources'] == adoption['sources'] == selected and
                peer['findings'] == adoption['findings'] == [] and
                peer['preparation'] == adoption['source_preparation'] == descriptor['healthy_preparation'] and
                peer['source_derivation'] == preparation['source_derivation'] == descriptor['healthy_derivation'] and
                adoption['schema'] == 'causes.healthy_harvest_source_root_adoption.v1' and
                adoption['accepted_source_for_prospective_migration'] is True and
                adoption['independent_review'] == descriptor['healthy_review'] and
                parents == preparation['parent_sources'] == adoption['parent_sources'] and
                parents['causes.campaign_supplement'] == intermediate_ref and
                derivation['only_early_gate_changed'] is True and derivation['default_opt_in'] is False and
                {row['module']: row['source'] for row in derivation['proofs']} == selected and
                {row['module']: row['parent'] for row in derivation['proofs']} == parents,
                'derived_healthy_second_transition')
        require(unchanged_helpers(old) == unchanged_helpers(intermediate) == unchanged_helpers(new),
                'derived_runtime_request_helpers_changed')
        for item in (descriptor, early, peer, preparation, adoption, derivation):
            nested(item)
        return [refs[path] for path in sorted(refs)]
    except (core.IntegrityError, KeyError, TypeError, ValueError, SyntaxError) as exc:
        if isinstance(exc, error):
            raise
        raise error('derived_healthy_compatibility_shape') from exc


class FrozenProbeAdapter:
    """Use unchanged derived preparation and native probe_measurement callbacks.

The only exception translation is between the separately loaded, source-equal
native IntegrityError classes. It is never a retry, expiry renewal or hold
relaxation. Source checks run before preparation and before/after each worker.
"""
    def __init__(self, adapter, operational, protocol, rows, *, scientific_source,
                 operational_source, source_check, operational_review=None,
                 execution_protocol=None, read=core.pinned):
        compatible_queue(adapter.supplement, operational, scientific_source,
                         operational_source, review=operational_review, read=read)
        if protocol['sources']['campaign_supplement'] != scientific_source:
            raise operational.IntegrityError('derived_runtime_frozen_protocol_source')
        self.adapter, self.operational = adapter, operational
        self.source_check = source_check
        if not callable(source_check):
            raise operational.IntegrityError('derived_runtime_source_callback_required')
        self.protocol = copy.deepcopy(protocol if execution_protocol is None else execution_protocol)
        self.rows = copy.deepcopy(rows)
        try:
            original_config, original_identity = adapter._protocol(protocol)
            self.config, identity = adapter._protocol(self.protocol)
        except adapter.supplement.IntegrityError as error:
            raise operational.IntegrityError(str(error)) from error
        if identity != original_identity or \
                {k:v for k,v in self.config.items() if k != 'endpoint'} != \
                {k:v for k,v in original_config.items() if k != 'endpoint'}:
            raise operational.IntegrityError('derived_runtime_semantic_protocol_changed')

    def prepare(self, job):
        self.source_check(job)
        if job['measurement_id'] not in self.rows:
            raise self.operational.IntegrityError('derived_runtime_unknown_job')
        try:
            return self.adapter.prepare_probe(job, self.rows[job['measurement_id']],
                                               self.protocol, self.config)
        except self.adapter.supplement.IntegrityError as error:
            if isinstance(error, self.operational.IntegrityError):
                raise
            raise self.operational.IntegrityError(str(error)) from error

    def worker(self, job, destination):
        self.source_check(job)
        try:
            return self.operational.probe_measurement(job, destination, self.prepare,
                                                       self.config, self.adapter.runner)
        finally:
            self.source_check(job)
