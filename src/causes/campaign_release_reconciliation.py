"""Release a waiting owner after one explicitly reviewed foreign allocation delta.

The original allocator and its immutable control remain unchanged. A pure preview
checks only its first block against the exact foreign additions. This does not
import foreign seeds into its ledger or establish future global uniqueness.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from causes import campaign_admission as admission
from causes import campaign_handoff as h
from causes.campaign_admission import (ContinuousAdmission, START, RELEASE, accepted,
    donor_cuts_clear, recent, require, validate_registration)
from causes.campaign_census import FIELDS, identity
from causes import corpus_allocation, corpus_continuation


CONFIG_SCHEMA = 'causes.release_reconciliation_configuration.v1'
EVIDENCE_SCHEMA = 'causes.release_reconciliation.v1'
GUARANTEE = 'initial_reserved_allocation_seeds_plus_own_cumulative_ledger'
LIMIT = 'First block only; foreign seeds are not imported and future global numeric uniqueness is not established.'


def equal(left, right):
    return h.encoded(left) == h.encoded(right)


def digest(value):
    return hashlib.sha256(h.encoded(value)).hexdigest()


def allocation_rows(value):
    require(value['schema'] == 'causes.corpus_allocation_reservation.v1', 'reconciliation_reservation_schema')
    rows = {}
    for raw in value['allocation_identities']:
        row = identity(raw)
        require(row['run_id'] not in rows, 'duplicate_reconciliation_identity')
        rows[row['run_id']] = row
    require(sorted(rows) == value['run_ids'], 'reconciliation_ID_union_changed')
    seeds = sorted({r[k] for r in rows.values() for k in FIELDS[1:] if type(r[k]) is int and r[k] > 0})
    require(equal(seeds, value['allocation_seeds']), 'reconciliation_seed_union_changed')
    return rows


def block_map(rows):
    result = {}
    for row in rows:
        key = (row['host'], row['file']['path'])
        require(key not in result, 'duplicate_reconciliation_block')
        result[key] = row
    return result


def reservation_delta(original, observed, foreign_blocks):
    """Typed exact union comparison; no clock, files, or allocation generation."""
    old, new = allocation_rows(original), allocation_rows(observed)
    require(set(old) <= set(new) and all(equal(row, new[rid]) for rid, row in old.items()),
            'original_typed_allocation_changed')
    require(equal(original['frozen_input_pins'], observed['frozen_input_pins']), 'reconciliation_frozen_pins_changed')
    before, after, additions = (block_map(value) for value in
        (original['persisted_full_blocks'], observed['persisted_full_blocks'], foreign_blocks))
    require(additions and not set(before) & set(additions), 'foreign_blocks_must_be_new')
    expected = {**before, **additions}
    require(set(after) == set(expected) and all(equal(after[key], row) for key, row in expected.items()),
            'unreviewed_foreign_block_change')
    added = {rid: new[rid] for rid in sorted(set(new) - set(old))}
    require(added, 'nonempty_foreign_delta_required')
    seeds = sorted(set(observed['allocation_seeds']) - set(original['allocation_seeds']))
    require(set(original['allocation_seeds']) <= set(observed['allocation_seeds']), 'old_allocation_seed_removed')
    return added, seeds, {
        'original_IDs': len(old), 'original_positive_seeds': len(original['allocation_seeds']),
        'original_blocks': len(before), 'frozen_inputs': len(original['frozen_input_pins']),
        'added_IDs': len(added), 'added_positive_seeds': len(seeds),
        'added_typed_rows_sha256': digest(added), 'added_IDs_sha256': digest(sorted(added)),
        'added_positive_seeds_sha256': digest(seeds),
        'frozen_input_pins_sha256': digest(original['frozen_input_pins']),
    }


def foreign_rows_from_captures(observed, foreign_blocks, added, seeds):
    """Read only the accepted census metadata, never the remote block bodies."""
    wanted = block_map(foreign_blocks)
    found, rows, locations = {}, {}, {}
    captures = []
    require(len(observed['host_cuts']) == len(observed['capture_pins']), 'foreign_capture_count')
    require(len({cut['host'] for cut in observed['host_cuts']}) == len(observed['host_cuts']), 'duplicate_foreign_host')
    for cut, ref in zip(observed['host_cuts'], observed['capture_pins'], strict=True):
        capture = h.checked(ref)
        require(capture['host'] == cut['host'], 'foreign_capture_host_changed')
        require(capture['schema'] == 'causes.allocation_metadata_census.v1'
                and capture['reads_actor_or_probe_outcomes'] is False, 'foreign_capture_schema')
        captures.append((cut['host'], capture))
        for item in capture['files']:
            key = (cut['host'], item['path'])
            if key not in wanted:
                continue
            require(key not in found and item['kind'] == 'persisted_extension_block', 'foreign_block_entry_not_unique')
            descriptor = {'host': cut['host'], 'file': {k:item[k] for k in ('path','bytes','sha256')},
                          'allocation_count': len(item['allocations'])}
            require(equal(descriptor, wanted[key]), 'foreign_block_fingerprint_changed')
            require(Path(item['path']).parent.name == 'blocks'
                    and Path(item['path']).parent.parent.name == '.dispatcher', 'foreign_block_path_changed')
            found[key] = descriptor
            locations[key] = item['root']
            for raw in item['allocations']:
                row = identity(raw); rid = row['run_id']
                require(rid not in rows, 'duplicate_foreign_block_ID')
                rows[rid] = row
    require(set(found) == set(wanted) and equal(rows, added), 'foreign_entries_do_not_equal_exact_added_union')
    union = sorted({row[k] for row in rows.values() for k in FIELDS[1:] if type(row[k]) is int and row[k] > 0})
    require(equal(union, seeds), 'foreign_seed_domains_not_exact_new_union')
    # Every appearance of an added ID must be in its reviewed block or an
    # actual attempt within the same host/root. No new location is inferred.
    location_by_id = {}
    for host, capture in captures:
        for item in capture['files']:
            if (host, item['path']) in wanted:
                for raw in item['allocations']:
                    location_by_id[raw['run_id']] = (host, item['root'])
    for host, capture in captures:
        for item in capture['files']:
            for raw in item['allocations']:
                rid = raw['run_id']
                if rid not in added:
                    continue
                require(equal(identity(raw), added[rid]), 'foreign_attempt_identity_changed')
                require((host, item['root']) == location_by_id[rid], 'foreign_ID_appeared_on_other_root')
                require((host, item['path']) in wanted or item['kind'] == 'actual_allocation',
                        'foreign_ID_from_unreviewed_source')
    return [{'host': row['host'], 'file': row['file'], 'root': locations[key],
             'allocation_count': row['allocation_count']} for key, row in sorted(found.items())]


def source_bindings(configuration):
    require(configuration['schema'] == CONFIG_SCHEMA and configuration['guarantee'] == GUARANTEE
            and configuration['limitation'] == LIMIT, 'reconciliation_scope_changed')
    for key, module in [('adapter_source', sys.modules[__name__]), ('native_source', admission)]:
        ref = configuration[key]
        h.checked_bytes(ref)
        require(hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == ref['sha256'],
                'loaded_reconciliation_source_changed:' + key)
    modules = {'causes.corpus_continuation': corpus_continuation, 'causes.corpus_allocation': corpus_allocation}
    require(set(configuration['allocator_sources']) == set(modules), 'exact_allocator_sources_required')
    for name, module in modules.items():
        ref = configuration['allocator_sources'][name]
        h.checked_bytes(ref)
        require(hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == ref['sha256'],
                'loaded_preview_allocator_changed')


def preview_first_block(control_reference, expected_binding, added, seeds):
    control = h.checked(control_reference)
    allocator = corpus_continuation.CorpusContinuation.from_control(control,
        h.checked(control['prototypes']), h.checked(control['config']))
    require(equal(allocator.binding, expected_binding), 'preview_original_binding_changed')
    block = allocator.block(0)
    require(block['block_index'] == 0 and block['previous_block_sha256'] is None,
            'preview_must_be_first_block')
    rows = [identity(job['allocation']) for job in block['jobs']]
    ids = [row['run_id'] for row in rows]
    ledger_seeds = [row['seed'] for row in block['seed_ledger'].values()]
    require(len(set(ids)) == len(ids) and len(set(ledger_seeds)) == len(ledger_seeds)
            and all(type(seed) is int and seed > 0 for seed in ledger_seeds), 'invalid_first_block_preview')
    require(set(ledger_seeds) == {row[k] for row in rows for k in FIELDS[1:]}, 'preview_all_seed_domains_required')
    require(not set(ids) & set(added), 'preview_foreign_ID_collision')
    require(not set(ledger_seeds) & set(seeds), 'preview_foreign_seed_collision')
    return {'block_index': 0, 'block_sha256': digest(block), 'binding': block['binding'],
            'previous_block_sha256': None, 'reserved_seeds_before_sha256': block['reserved_seeds_before_sha256'],
            'typed_allocation_rows_sha256': digest(rows), 'seed_ledger_sha256': digest(block['seed_ledger']),
            'run_ids': ids, 'allocation_seeds': sorted(ledger_seeds),
            'counts': {'IDs': len(ids), 'positive_seeds': len(ledger_seeds)},
            'persisted': False, 'claims_created': False, 'foreign_seeds_imported': False}


def compose(configuration_reference):
    """Pure retained-input composition; creates no files, claims, or authority."""
    config = h.checked(configuration_reference)
    source_bindings(config)
    policy = h.checked(config['policy']); control = h.checked(config['control'])
    require(config['control'] == policy['continuation']['control']
            and control['reservation'] == config['original_reservation'], 'original_control_binding_changed')
    require(policy['source_sha256'] == config['native_source']['sha256'], 'native_policy_source_changed')
    original, observed = (h.checked(config[key]) for key in ('original_reservation','observed_reservation'))
    added, seeds, delta = reservation_delta(original, observed, config['foreign_blocks'])
    require(equal(delta, config['expected_delta']), 'configured_foreign_delta_changed')
    locations = foreign_rows_from_captures(observed, config['foreign_blocks'], added, seeds)
    preview = preview_first_block(config['control'], policy['continuation']['binding'], added, seeds)
    require(equal(preview['counts'], config['expected_preview_counts']), 'preview_count_changed')
    return {'schema': EVIDENCE_SCHEMA, 'configuration': configuration_reference,
            'policy': config['policy'], 'control': config['control'], 'launch': config['launch'],
            'original_reservation': config['original_reservation'], 'observed_reservation': config['observed_reservation'],
            'foreign_blocks': config['foreign_blocks'], 'foreign_locations': locations, 'delta': delta,
            'preview': preview, 'allocator_sources': config['allocator_sources'],
            'guarantee': GUARANTEE, 'limitation': LIMIT, 'runtime_actions': False}


def fresh_matches_reconciliation(original, fresh, observed, configuration, reconciliation):
    """Retain exact current freshness and reject any further union/block growth."""
    added, seeds, delta = reservation_delta(original, fresh, configuration['foreign_blocks'])
    require(equal(delta, reconciliation['delta']) and equal(delta, configuration['expected_delta']),
            'fresh_delta_not_exact_reviewed_delta')
    require(equal(allocation_rows(fresh), allocation_rows(observed)), 'fresh_union_not_exact_reconciled_union')
    for key in ('configuration', 'composition_source'):
        require(equal(fresh[key], observed[key]), 'fresh_census_source_or_scope_changed')
    require([c['host'] for c in fresh['host_cuts']] == [c['host'] for c in observed['host_cuts']], 'fresh_host_scope_changed')
    for cut in fresh['host_cuts']:
        recent(cut['started_at']); recent(cut['finished_at'])
    preview = reconciliation['preview']
    require(equal(preview['counts'], configuration['expected_preview_counts'])
            and preview['block_index'] == 0 and preview['previous_block_sha256'] is None
            and preview['persisted'] is False and preview['claims_created'] is False
            and preview['foreign_seeds_imported'] is False, 'reviewed_preview_scope_changed')
    require(not set(preview['run_ids']) & set(added), 'preview_foreign_ID_collision')
    require(not set(preview['allocation_seeds']) & set(seeds), 'preview_foreign_seed_collision')
    require(preview['reserved_seeds_before_sha256'] == digest(sorted(original['allocation_seeds'])),
            'preview_initial_reservation_changed')
    return True


class ReconciledRelease(ContinuousAdmission):
    """Inherit static/live/release; add explicit evidence and authority bindings."""
    def __init__(self, policy, inputs, authority, configuration, source_review, reconciliation, reconciliation_review):
        super().__init__(policy, inputs, authority)
        self.configuration_ref = configuration
        self.source_review_ref = source_review
        self.reconciliation_ref = reconciliation
        self.reconciliation_review_ref = reconciliation_review

    def adapter_bindings(self, authority_value=None):
        config = h.checked(self.configuration_ref)
        source_bindings(config)
        review = h.checked(self.source_review_ref)
        require(review.get('accepted_continuous_release_reconciliation_source') is True
                and review.get('source') == config['adapter_source']
                and review.get('configuration') == self.configuration_ref, 'unaccepted_release_reconciliation_source')
        rec = h.checked(self.reconciliation_ref); accepted_review = h.checked(self.reconciliation_review_ref)
        require(accepted_review.get('accepted_continuous_release_reconciliation') is True
                and accepted_review.get('reconciliation') == self.reconciliation_ref
                and accepted_review.get('first_block_preview_replayed') is True
                and accepted_review.get('foreign_census_entries_reconstructed') is True,
                'unaccepted_foreign_reconciliation')
        require(rec['schema'] == EVIDENCE_SCHEMA and rec['configuration'] == self.configuration_ref
                and rec['guarantee'] == GUARANTEE and rec['limitation'] == LIMIT
                and rec['runtime_actions'] is False, 'reconciliation_receipt_scope_changed')
        require(config['policy'] == self.policy_ref and config['control'] == self.policy['continuation']['control']
                and config['launch'] == self.inputs['launch']
                and config['original_reservation'] == self.inputs['precreation_reservation']['evidence'],
                'reconciliation_original_owner_changed')
        for key in ('policy','control','launch','original_reservation','observed_reservation','foreign_blocks','allocator_sources'):
            require(equal(rec[key], config[key]), 'reconciliation_binding_changed:' + key)
        require(equal(rec['preview']['binding'], self.policy['continuation']['binding']), 'preview_policy_binding_changed')
        if authority_value is not None:
            expected = {'adapter_source': config['adapter_source'], 'configuration': self.configuration_ref,
                        'source_review': self.source_review_ref, 'reconciliation': self.reconciliation_ref,
                        'reconciliation_review': self.reconciliation_review_ref}
            require(equal(authority_value.get('release_reconciliation'), expected), 'authority_reconciliation_changed')
        return config, rec

    def reconciled_reservation(self, old, fresh):
        config, rec = self.adapter_bindings()
        review = h.checked(self.inputs['fresh_reservation']['review'])
        require(review.get('accepted_continuous_foreign_reconciled_reservation') is True
                and review.get('reconciliation') == self.reconciliation_ref
                and review.get('original_reservation_equality') is False,
                'fresh_reservation_requires_explicit_reconciliation_review')
        return fresh_matches_reconciliation(old, fresh, h.checked(config['observed_reservation']), config, rec)

    def authority(self, action):
        require(action == RELEASE, 'reconciliation_release_only')
        native = super().authority(action)
        def validate(value):
            self.adapter_bindings(value)
            result = native.validate_policy(value)
            self.adapter_bindings(value)
            return result
        return h.Authority(native.reference, native.action, native.pause_paths, validate)

    def start(self, execute=False):
        raise ValueError('reconciliation_release_only')

    def evidence(self, action):
        require(action == RELEASE, "reconciliation_release_only")
        self.adapter_bindings()
        p, x = self.policy, self.inputs
        for key in ('prior_custody','precreation_reservation','fresh_reservation','foundation'):
            rule=p['acceptance_rules'][key]
            accepted(x[key],rule['flag'],rule['binding_key'])
        complete=h.checked(x['prior_custody']['evidence'])
        require(complete['host_transfer']==p['host_transfer'] and set(complete['roots'])=={r['output_root'] for r in p['predecessors']},
                'finite_custody_scope_changed')
        for key in ('all_expected_terminal_custody','all_closed_verified','technical_missingness_preserved','no_unresolved_terminal_records'):
            require(complete[key] is True, 'prior_custody_not_complete:'+key)
        old, fresh = h.checked(x['precreation_reservation']['evidence']), h.checked(x['fresh_reservation']['evidence'])
        self.reconciled_reservation(old, fresh)
        require([c['host'] for c in fresh['host_cuts']]==p['census_hosts'], 'both_host_census_required')
        scope=h.checked(x['fresh_reservation']['review'])
        require(scope['census_config']==p['census_configs']['before_start' if action==START else 'waiting_release'], 'census_root_scope_changed')
        foundation=h.checked(x['foundation']['evidence']);recent(foundation['at'])
        require(foundation['host_transfer']==p['host_transfer'] and foundation['provider']==p['provider'], 'foundation_binding_changed')
        for key in ('legacy_two_owners_healthy','provider_tunnels_healthy','original_transfer_present','unused_MI_output_absent',
                    'donor_histories_clear','native_no_active_owned_work'):
            require(foundation[key] is True, 'foundation_not_clear:'+key)
        require(foundation['legacy_owners']==p['legacy_owners'], 'legacy_owner_binding_changed')
        donor_cuts_clear(p['history'],p['donor_histories'],foundation['donor_cuts'])
        for ref in foundation['evidence_pins']:h.checked_bytes(ref)
        if action==RELEASE:
            ready=accepted(x['startup'], 'accepted_continuous_startup_watch','readiness');recent(ready['at'])
            launch=h.checked(x['launch'])
            require(ready['launch']==x['launch'] and ready['owned_barrier']==launch['owned_barrier'], 'startup_owner_binding_changed')
            for key in ('zero_initial_claims','zero_persisted_blocks','legacy_MI_Tex_healthy','mandatory_JP_companion_valid','all_13_MI_roots_requested','all_4_JP_roots_requested','same_owned_watcher_successor'):
                require(ready[key] is True, 'startup_gate_missing:'+key)
            for key in ('registration','authorization_pointer','monitor_policy','watcher_cycle','MI_sync_roots','JP_sync_roots'):
                h.checked_bytes(ready[key])
            registration=h.checked(ready['registration']);pointer=h.checked(ready['authorization_pointer'])
            require(pointer['registration']==ready['registration'] and registration['controller']['process']==launch['process']
                    and registration['controller']['launch_receipt']==x['launch'], 'startup_registration_changed')
            validate_registration(registration,h.checked(ready['monitor_policy']),reader=h.checked)
            require(registration['donor_histories']==p['donor_histories'], 'startup_donor_binding_changed')
        return complete


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    build = sub.add_parser('compose')
    build.add_argument('--configuration', required=True, help='Exact JSON pin')
    build.add_argument('--output', required=True, help='Exclusive receipt directory')
    release = sub.add_parser('release')
    for key in ('policy','inputs','authority','configuration','source-review','reconciliation','reconciliation-review'):
        release.add_argument('--' + key, required=True, help='Exact JSON pin')
    release.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'compose':
        value = compose(json.loads(args.configuration))
        result = h.save_new(h.new_record_directory(args.output), 'RECONCILIATION.json', value)
    else:
        operator = ReconciledRelease(*(json.loads(getattr(args, key.replace('-', '_'))) for key in
            ('policy','inputs','authority','configuration','source-review','reconciliation','reconciliation-review')))
        result = operator.release(execute=args.execute)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
