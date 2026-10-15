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
READY_CONTINUATION = 'causes.derived_ready_harvest_source_continuation.v1'


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


AFTER_DERIVED = 'causes.original_after_derived_source_transition.v1'


def validate_after_derived(reference, *, predecessor, sources, read, mixed_owner):
    """Join the actual completed derived invocation to an explicit source successor.

    The native healthy queue stays identical. The changed completed-actor
    resolver, lineage/helper, driver and binder require a separate source peer
    and root adoption. The first adopted invocation's original-to-derived
    continuation is verified against its genuine original predecessor. Accepted
    same-adoption suffixes may intervene, retaining that provenance and exact
    native/helper bytes. Their own closed peers and sources remain required.
    This does not authorize or supply a derived suffix executor.
    """
    try:
        value, refs = mixed_owner._reader(read, 128 * 1024**2)
        transition = value(reference)
        require(transition['schema'] == AFTER_DERIVED and transition['kind'] == KIND,
                'after_derived_explicit_transition')
        partition = mixed_owner.resolve(transition['owner_partition'], predecessor=predecessor,
            source=sources['mixed_owner'], read=read)
        head = value(predecessor['actual']); prior = value(head['plan'])
        require(head['plan'] == head['native_invocations'][-1]['plan']
                and 'derived_bank' in prior and prior['derived_bank'] == partition['derived_adoption']
                and prior['continuation_scope'] in ('adopted_derived_bank','derived_bank_remaining')
                and prior.get('retain_admission_after_healthy_harvest') is True
                and transition['predecessor_plan'] == head['plan']
                and transition['previous_lineage'] == prior['native_source_migration']
                and transition['previous_source'] == prior['sources']['native_queue']
                and transition['previous_runtime_completed_actor_source'] == prior['sources']['completed_actor']
                and transition['source'] == sources['native_queue'] == prior['sources']['native_queue']
                and transition['completed_actor_source'] == sources['completed_actor']
                and transition['mixed_owner_source'] == sources['mixed_owner']
                and transition['lineage_source'] == sources['source_lineage']
                and transition['driver_source'] == sources['driver'], 'after_derived_exact_endpoint')
        peer, adoption = value(transition['source_review']), value(transition['root_source_adoption'])
        selected = {key:sources[key] for key in ('native_queue','completed_actor','mixed_owner','source_lineage','driver')}
        selected['binder'] = transition['binder_source']
        require(peer.get('accepted_original_after_derived_source') is True
                and peer.get('findings') == [] and peer['sources'] == selected
                and peer['previous_completed_actor_source'] == prior['sources']['completed_actor']
                and adoption['schema'] == 'causes.original_after_derived_source_adoption.v1'
                and adoption['accepted_source_for_prospective_transition'] is True
                and adoption['sources'] == selected and adoption['independent_review'] == transition['source_review']
                and adoption['findings'] == [], 'after_derived_source_acceptance')
        derived_links = partition['links']
        require(derived_links and all(link['kind'] == 'derived' for link in derived_links)
                and derived_links[-1]['custody'] == predecessor, 'after_derived_only_same_adoption')
        first = value(derived_links[0]['plan'])
        require(first['continuation_scope'] == 'adopted_derived_bank'
                and first['predecessor_custody'] == partition['original_base'], 'after_derived_first_invocation')
        retained_sources = []
        for index, link in enumerate(derived_links):
            step = value(link['plan'])
            require(step['derived_bank'] == partition['derived_adoption']
                    and step['continuation_scope'] == ('derived_bank_remaining' if index else 'adopted_derived_bank')
                    and step['native_source_migration'] == first['native_source_migration']
                    and step.get('retain_admission_after_healthy_harvest') is True
                    and all(step['sources'][key] == first['sources'][key]
                            for key in ('native_queue','completed_actor')),
                    'after_derived_continuation_sources')
            # resolve() already joins each accepted closure to its exact driver,
            # predecessor, suffix, failure state and complete invocation list.
            for ref in step['sources'].values():
                require(type(ref) is dict and set(ref) == {'path','bytes','sha256'}
                        and type(ref['bytes']) is int and 0 <= ref['bytes'] <= 8 * 1024**2,
                        'after_derived_retained_source_pin')
                data = read(ref)
                require(type(data) is bytes and len(data) == ref['bytes']
                        and hashlib.sha256(data).hexdigest() == ref['sha256'],
                        'after_derived_retained_source_bytes')
                retained_sources.append(ref)
        lineage = validate_continuation(first['native_source_migration'],
            predecessor=first['predecessor_custody'], sources=first['sources'], read=read)
        required = list(refs.values()) + partition['required_pins'] + lineage['required_pins']
        required.extend(retained_sources)
        for ref in selected.values():
            data = read(ref)
            require(type(data) is bytes and len(data) == ref['bytes']
                    and hashlib.sha256(data).hexdigest() == ref['sha256'], 'after_derived_source_bytes')
            required.append(ref)
        unique = {}
        for ref in required:
            require(ref['path'] not in unique or unique[ref['path']] == ref,
                    'after_derived_conflicting_pin')
            unique[ref['path']] = ref
        return dict(kind='original_after_derived', original_migration=lineage['original_migration'],
                    predecessor=predecessor, owner_partition=transition['owner_partition'],
                    first_derived_plan=derived_links[0]['plan'], derived_invocations=len(derived_links),
                    required_pins=list(unique.values()))
    except mixed_owner.MixedOwnerError as error:
        raise SourceLineageError(str(error)) from error
    except (KeyError, TypeError, IndexError) as error:
        raise SourceLineageError('after_derived_shape') from error



def validate_ready_continuation(reference, *, predecessor, sources, read,
                                validate_migration):
    """Retain one accepted original ready migration at the first derived handoff.

    The caller loads the separately pinned migration validator and binds its
    callable here. That validator keeps its original parent and ancestry rules.
    Later derived suffixes validate this same first handoff, not a new migration.
    This function grants no job, owner, storage or runtime authority.
    """
    require(callable(read) and callable(validate_migration), 'ready_continuation_reader')
    refs, cache = {}, {}
    total = 0

    def raw(ref):
        nonlocal total
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'},
                'ready_continuation_pin')
        path, size, sha = ref['path'], ref['bytes'], ref['sha256']
        require(type(path) is str and path.startswith('/') and '\x00' not in path and
                str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts and
                type(size) is int and 0 <= size <= 8 * 1024**2 and
                type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha) is not None,
                'ready_continuation_pin_bound')
        require(path not in refs or refs[path] == ref, 'ready_continuation_conflicting_pin')
        if path not in refs:
            require(len(refs) < 4096 and total + size <= 512 * 1024**2,
                    'ready_continuation_read_bound')
            body = read(ref)
            require(type(body) is bytes and len(body) == size and
                    hashlib.sha256(body).hexdigest() == sha, 'ready_continuation_pin_bytes')
            refs[path], cache[path] = dict(ref), body
            total += size
        return cache[path]

    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'ready_continuation_duplicate_key')
            result[key] = item
        return result

    def value(ref):
        def invalid(_):
            raise SourceLineageError('ready_continuation_nonfinite_json')
        result = json.loads(raw(ref), object_pairs_hook=unique, parse_constant=invalid)
        require(type(result) is dict, 'ready_continuation_object')
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

    try:
        step = value(reference)
        require(set(step) == {'schema', 'kind', 'predecessor', 'predecessor_plan',
                'previous_migration', 'source', 'completed_actor_source', 'driver_source',
                'binder_source', 'lineage_source', 'migration_validator_source',
                'source_preparation', 'source_derivation', 'source_review',
                'root_source_adoption'}, 'ready_continuation_shape')
        require(step['schema'] == READY_CONTINUATION and
                step['kind'] == 'retain_admission_after_ready_harvest' and
                step['predecessor'] == predecessor and
                type(predecessor) is dict and set(predecessor) == {'actual', 'review'},
                'ready_continuation_descriptor')
        actual, closed_peer = value(predecessor['actual']), value(predecessor['review'])
        require(closed_peer.get('accepted_finite_supplement_closed_custody') is True and
                closed_peer.get('accepted_completed_actor_saved_closure') is True and
                closed_peer['actual'] == predecessor['actual'] and closed_peer['findings'] == [] and
                closed_peer['source'] == actual['source'] and
                closed_peer['all_original_attempts_retained'] is True and
                closed_peer['original_denominator_preserved'] is True and
                closed_peer['qualification_findings'] == actual['qualification_findings'],
                'ready_continuation_closed_peer')
        require(actual['schema'] == 'causes.finite_supplement_tranche_closed.v1' and
                type(actual['active']) is int and actual['active'] == 0 and
                actual['unstarted_ids'] == [] and actual['ambiguous'] == [] and
                actual['unresolved_claimed_ids'] == [] and actual['quarantined'] is False and
                actual['clean_native_ending'] is True and
                actual['all_original_attempts_retained'] is True and
                actual['original_denominator_preserved'] is True and actual['native_invocations'] and
                actual['native_invocations'][-1]['plan'] == actual['plan'] == step['predecessor_plan'],
                'ready_continuation_closed_original')
        prior = value(actual['plan'])
        require('derived_bank' not in prior and 'original_reservation_transfer' not in prior and
                prior.get('retain_admission_after_healthy_harvest') is True and
                prior.get('retain_admission_after_ready_harvest') is True and
                prior['native_source_migration'] == step['previous_migration'],
                'ready_continuation_original_plan')
        require(step['source'] == sources['native_queue'] == prior['sources']['native_queue'] and
                step['completed_actor_source'] == sources['completed_actor'] == prior['sources']['completed_actor'] and
                step['driver_source'] == sources['driver'] and
                step['lineage_source'] == sources['source_lineage'] and
                step['migration_validator_source'] == sources['ready_migration'] ==
                    prior['sources']['source_lineage'], 'ready_continuation_endpoints')
        for key in ('native_queue', 'completed_actor', 'driver', 'source_lineage',
                    'derived_runtime', 'ready_migration'):
            raw(sources[key])
        migration = validate_migration(prior, read=raw)
        require(type(migration) is dict and
                migration.get('kind') == 'authenticated_ready_harvest_migration' and
                migration.get('runtime_authorized') is False and
                type(migration.get('required_pins')) is list and
                step['previous_migration'] in migration['required_pins'],
                'ready_continuation_migration_proof')
        for ref in migration['required_pins']:
            raw(ref)
        prep = value(step['source_preparation']); derivation = value(step['source_derivation'])
        peer = value(step['source_review']); adoption = value(step['root_source_adoption'])
        selected = {key: sources[key] for key in ('native_queue', 'completed_actor',
                    'driver', 'source_lineage', 'derived_runtime', 'ready_migration')}
        selected['binder'] = step['binder_source']
        require(peer.get('accepted_derived_ready_harvest_runtime_sources') is True and
                adoption.get('accepted_derived_ready_harvest_runtime_sources') is True and
                adoption['root_authorized'] is True and adoption['runtime_authorized'] is False and
                peer['findings'] == adoption['findings'] == [] and
                all(obj['first_runtime_sources'] == selected for obj in (prep, peer, adoption)) and
                adoption['independent_review'] == step['source_review'] and
                all(obj['source_preparation'] == step['source_preparation'] for obj in (peer, adoption)) and
                all(obj['source_derivation'] == step['source_derivation'] for obj in (prep, peer, adoption)) and
                derivation['only_ready_compatibility_and_lineage_changed'] is True and
                derivation['scientific_identity_preserved'] is True,
                'ready_continuation_source_acceptance')
        for item in (step, prep, derivation, peer, adoption):
            nested(item)
        return dict(kind='same_source_ready_continuation',
                    original_migration=dict(step['previous_migration']),
                    predecessor_plan=dict(actual['plan']), migration=migration,
                    required_pins=[refs[path] for path in sorted(refs)],
                    runtime_authorized=False)
    except (KeyError, TypeError, IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceLineageError('ready_continuation_shape') from exc

AFTER_DERIVED_READY = 'causes.original_after_derived_ready_source_transition.v1'


def validate_after_derived_ready(reference, *, predecessor, sources, read, mixed_owner,
                                 validate_migration):
    """Join a complete derived owner to an explicitly accepted ready successor.

    The historical first invocation retains its own ready source map and
    migration validator. A separately adopted mixed completed actor can change
    at this boundary; the queue, scientific rows and prior lineage cannot.
    This saved-metadata check neither authorizes a runtime nor invents closure.
    """
    require(callable(validate_migration), 'after_derived_ready_validator')
    try:
        value, refs = mixed_owner._reader(read, 128 * 1024**2)
        transition = value(reference)
        require(set(transition) == {'schema', 'kind', 'owner_partition',
                'predecessor_plan', 'previous_lineage', 'previous_source',
                'previous_runtime_completed_actor_source', 'source',
                'completed_actor_source', 'mixed_owner_source', 'lineage_source',
                'driver_source', 'binder_source', 'migration_validator_source',
                'source_review', 'root_source_adoption'} and
                transition['schema'] == AFTER_DERIVED_READY and
                transition['kind'] == 'retain_admission_after_ready_harvest',
                'after_derived_ready_transition')
        partition = mixed_owner.resolve(transition['owner_partition'], predecessor=predecessor,
            source=sources['mixed_owner'], read=read)
        head = value(predecessor['actual']); prior = value(head['plan'])
        require(head['plan'] == head['native_invocations'][-1]['plan']
                and 'derived_bank' in prior and prior['derived_bank'] == partition['derived_adoption']
                and prior['continuation_scope'] in ('adopted_derived_bank','derived_bank_remaining')
                and prior.get('retain_admission_after_healthy_harvest') is True
                and prior.get('retain_admission_after_ready_harvest') is True
                and transition['predecessor_plan'] == head['plan']
                and transition['previous_lineage'] == prior['native_source_migration']
                and transition['previous_source'] == prior['sources']['native_queue']
                and transition['previous_runtime_completed_actor_source'] == prior['sources']['completed_actor']
                and transition['source'] == sources['native_queue'] == prior['sources']['native_queue']
                and transition['completed_actor_source'] == sources['completed_actor']
                and transition['mixed_owner_source'] == sources['mixed_owner']
                and transition['lineage_source'] == sources['source_lineage']
                and transition['driver_source'] == sources['driver']
                and transition['migration_validator_source'] == sources['ready_migration'] ==
                    prior['sources']['ready_migration'], 'after_derived_exact_endpoint')
        peer, adoption = value(transition['source_review']), value(transition['root_source_adoption'])
        selected = {key:sources[key] for key in ('native_queue','completed_actor','mixed_owner','source_lineage','driver','ready_migration')}
        selected['binder'] = transition['binder_source']
        require(peer.get('accepted_original_after_derived_source') is True
                and peer.get('accepted_original_after_derived_ready_source') is True
                and peer.get('findings') == [] and peer['sources'] == selected
                and peer['previous_completed_actor_source'] == prior['sources']['completed_actor']
                and adoption['schema'] == 'causes.original_after_derived_ready_source_adoption.v1'
                and adoption.get('accepted_original_after_derived_ready_source') is True
                and adoption.get('root_authorized') is True and adoption.get('runtime_authorized') is False
                and adoption['accepted_source_for_prospective_transition'] is True
                and adoption['sources'] == selected and adoption['independent_review'] == transition['source_review']
                and adoption['findings'] == [], 'after_derived_source_acceptance')
        derived_links = partition['links']
        require(derived_links and all(link['kind'] == 'derived' for link in derived_links)
                and derived_links[-1]['custody'] == predecessor, 'after_derived_only_same_adoption')
        first = value(derived_links[0]['plan'])
        require(first['continuation_scope'] == 'adopted_derived_bank'
                and first['predecessor_custody'] == partition['original_base'], 'after_derived_first_invocation')
        retained_sources = []
        for index, link in enumerate(derived_links):
            step = value(link['plan'])
            require(step['derived_bank'] == partition['derived_adoption']
                    and step['continuation_scope'] == ('derived_bank_remaining' if index else 'adopted_derived_bank')
                    and step['native_source_migration'] == first['native_source_migration']
                    and step.get('retain_admission_after_healthy_harvest') is True
                    and step.get('retain_admission_after_ready_harvest') is True
                    and all(step['sources'][key] == first['sources'][key]
                            for key in ('native_queue','completed_actor','ready_migration')),
                    'after_derived_continuation_sources')
            # resolve() already joins each accepted closure to its exact driver,
            # predecessor, suffix, failure state and complete invocation list.
            for ref in step['sources'].values():
                require(type(ref) is dict and set(ref) == {'path','bytes','sha256'}
                        and type(ref['bytes']) is int and 0 <= ref['bytes'] <= 8 * 1024**2,
                        'after_derived_retained_source_pin')
                data = read(ref)
                require(type(data) is bytes and len(data) == ref['bytes']
                        and hashlib.sha256(data).hexdigest() == ref['sha256'],
                        'after_derived_retained_source_bytes')
                retained_sources.append(ref)
        lineage = validate_ready_continuation(first['native_source_migration'],
            predecessor=first['predecessor_custody'], sources=first['sources'], read=read,
            validate_migration=validate_migration)
        required = list(refs.values()) + partition['required_pins'] + lineage['required_pins']
        required.extend(retained_sources)
        for ref in selected.values():
            data = read(ref)
            require(type(data) is bytes and len(data) == ref['bytes']
                    and hashlib.sha256(data).hexdigest() == ref['sha256'], 'after_derived_source_bytes')
            required.append(ref)
        unique = {}
        for ref in required:
            require(ref['path'] not in unique or unique[ref['path']] == ref,
                    'after_derived_conflicting_pin')
            unique[ref['path']] = ref
        return dict(kind='original_after_derived_ready', runtime_authorized=False, original_migration=lineage['original_migration'],
                    predecessor=predecessor, owner_partition=transition['owner_partition'],
                    first_derived_plan=derived_links[0]['plan'], derived_invocations=len(derived_links),
                    required_pins=list(unique.values()))
    except mixed_owner.MixedOwnerError as error:
        raise SourceLineageError(str(error)) from error
    except (KeyError, TypeError, IndexError) as error:
        raise SourceLineageError('after_derived_shape') from error
