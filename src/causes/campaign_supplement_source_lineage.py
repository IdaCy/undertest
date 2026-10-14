"""Verify retained source lineage for an unchanged operational continuation.

This is a pure saved-metadata check using a caller-supplied pinned byte reader.
It neither authorizes execution nor replaces the caller's closed-custody, job,
failure, ownership, capacity, storage, or per-request integrity checks. The
initial source migration stays a distinct event; later continuations may only
retain its exact native queue and completed-actor source pins.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re


MIGRATION = 'causes.original_transfer_healthy_harvest_migration.v1'
CONTINUATION = 'causes.original_transfer_healthy_harvest_continuation.v1'
KIND = 'retain_admission_after_healthy_harvest'


class SourceLineageError(ValueError):
    """The retained source chain does not describe an exact continuation."""


def require(value, reason):
    if not value:
        raise SourceLineageError(reason)


def validate_continuation(reference, *, predecessor, sources, read, max_links=64):
    """Return the original migration and every required immutable pin.

    ``predecessor`` is the accepted closed actual/review descriptor consumed by
    the caller. Each retained plan must point to that plan's genuine accepted
    predecessor, and to the exact previous lineage record. No new source
    migration is inferred from an already migrated predecessor. The injected
    reader owns filesystem identity checks and must not execute referenced code.
    All pins are size/hash checked here as well, before their JSON is trusted.
    """
    require(type(max_links) is int and 1 <= max_links <= 64, 'lineage_depth_limit')
    require(callable(read), 'lineage_reader_required')
    refs = {}

    def raw(ref):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}, 'lineage_pin')
        path = ref['path']
        require(type(path) is str and path.startswith('/') and '\x00' not in path and
                str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts and
                type(ref['bytes']) is int and 0 <= ref['bytes'] <= 8 * 1024**2 and
                type(ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}', ref['sha256']) is not None,
                'lineage_pin_shape_or_bound')
        require(path not in refs or refs[path] == ref, 'lineage_conflicting_pin')
        data = read(ref)
        require(type(data) is bytes and len(data) == ref['bytes'] and
                hashlib.sha256(data).hexdigest() == ref['sha256'], 'lineage_pin_bytes')
        refs[path] = dict(ref)
        return data

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'lineage_duplicate_json_key')
            value[key] = item
        return value

    def value(ref):
        def invalid_constant(_):
            raise SourceLineageError('lineage_nonfinite_json')
        try:
            result = json.loads(raw(ref), object_pairs_hook=unique_object,
                                parse_constant=invalid_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceLineageError('lineage_json') from error
        require(type(result) is dict, 'lineage_object')
        return result

    def rehash_nested(item):
        if type(item) is dict:
            if set(item) == {'path', 'bytes', 'sha256'}:
                raw(item)
            else:
                for child in item.values():
                    rehash_nested(child)
        elif type(item) is list:
            for child in item:
                rehash_nested(child)

    def closed_plan(descriptor):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'lineage_closed_descriptor')
        actual, peer = value(descriptor['actual']), value(descriptor['review'])
        require(peer.get('accepted_finite_supplement_closed_custody') is True and
                peer.get('actual') == descriptor['actual'] and peer.get('findings') == [],
                'lineage_closed_peer')
        require(actual['native_invocations'] and
                actual['native_invocations'][-1]['plan'] == actual['plan'],
                'lineage_immediate_plan')
        return actual['plan'], value(actual['plan'])

    def endpoint(step, plan_ref, prior, current_sources):
        require(step['kind'] == KIND and step['predecessor_plan'] == plan_ref and
                step['previous_source'] == prior['sources']['native_queue'] and
                step['previous_runtime_completed_actor_source'] == prior['sources']['completed_actor'] and
                step['source'] == current_sources['native_queue'] and
                step['completed_actor_source'] == current_sources['completed_actor'] and
                step['driver_source'] == current_sources['driver'], 'lineage_endpoint')
        rehash_nested(step)

    def original(step_ref, step, applied):
        require(step['schema'] == MIGRATION and applied.get('retain_admission_after_healthy_harvest') is True and
                'derived_bank' not in applied and applied['native_source_migration'] == step_ref,
                'lineage_original_migration')
        prior_ref, prior = closed_plan(applied['predecessor_custody'])
        endpoint(step, prior_ref, prior, applied['sources'])
        peer = value(step['source_review']); adoption = value(step['root_source_adoption'])
        preparation = value(step['source_preparation']); derivation = value(step['source_derivation'])
        selected = {'causes.campaign_supplement': step['source'],
                    'causes.campaign_supplement_completed_actor': step['completed_actor_source']}
        require(peer.get('accepted_healthy_harvest_continuation_source') is True and
                peer.get('accepted_completed_actor_healthy_harvest_source') is True and
                peer['sources'] == preparation['sources'] == adoption['sources'] == selected and
                peer['findings'] == adoption['findings'] == [] and
                peer['preparation'] == adoption['source_preparation'] == step['source_preparation'] and
                peer['source_derivation'] == preparation['source_derivation'] == step['source_derivation'] and
                adoption['schema'] == 'causes.healthy_harvest_source_root_adoption.v1' and
                adoption['accepted_source_for_prospective_migration'] is True and
                adoption['independent_review'] == step['source_review'] and
                peer['parent_sources'] == adoption['parent_sources'] == preparation['parent_sources'],
                'lineage_original_acceptance')
        parents = peer['parent_sources']
        require(parents['causes.campaign_supplement'] == step['previous_source'] and
                parents['causes.campaign_supplement_completed_actor'] == step['accepted_public_dependency'] and
                derivation['only_early_gate_changed'] is True and derivation['default_opt_in'] is False and
                {row['module']: row['source'] for row in derivation['proofs']} == selected and
                {row['module']: row['parent'] for row in derivation['proofs']} == parents,
                'lineage_original_ancestry')
        # Retain the historical source references, including differing prior
        # runtime and accepted public-helper ancestry, without relabeling either.
        rehash_nested(peer); rehash_nested(adoption); rehash_nested(preparation); rehash_nested(derivation)

    try:
        step_ref = reference; step = value(step_ref)
        require(step.get('schema') == CONTINUATION, 'lineage_continuation_required')
        origin = step['original_migration']; raw(origin)
        plan_ref, prior = closed_plan(predecessor)
        current_sources = sources
        visited = set()
        for depth in range(1, max_links + 1):
            require(step_ref['path'] not in visited, 'lineage_cycle')
            visited.add(step_ref['path'])
            require(step['schema'] == CONTINUATION and step['original_migration'] == origin,
                    'lineage_original_changed')
            endpoint(step, plan_ref, prior, current_sources)
            require(step['source'] == step['previous_source'] and
                    step['completed_actor_source'] == step['previous_runtime_completed_actor_source'],
                    'lineage_sources_changed')
            require(prior.get('retain_admission_after_healthy_harvest') is True and
                    'derived_bank' not in prior and step['previous_migration'] == prior['native_source_migration'],
                    'lineage_previous_record')
            parent_ref = step['previous_migration']; parent = value(parent_ref)
            if parent['schema'] == MIGRATION:
                require(parent_ref == origin, 'lineage_original_changed')
                original(parent_ref, parent, prior)
                return dict(kind='same_source_continuation', original_migration=dict(origin),
                            continuation_count=depth,
                            required_pins=[refs[path] for path in sorted(refs)])
            current_sources = prior['sources']
            plan_ref, prior = closed_plan(prior['predecessor_custody'])
            step_ref, step = parent_ref, parent
        raise SourceLineageError('lineage_depth_exceeded')
    except (KeyError, TypeError, IndexError) as error:
        raise SourceLineageError('lineage_shape') from error
