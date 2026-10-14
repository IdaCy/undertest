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
    """Accept literal code or the separately reviewed early-harvest successor.

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
