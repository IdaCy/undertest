"""Deterministic additional corpus blocks; no runtime or admission authority.

The reservation covers numeric allocation seeds at its retained census cut.
Other controllers' later allocations and derived request seeds are outside that
scope. Each block records its own seed ledger and the preceding block digest.
"""

import copy
import hashlib
import json
from pathlib import Path
import re

from causes.corpus_allocation import (
    MODELS, SEED_KEYS, SEED_MODULUS, SeedLedger, admissible_cells,
    chosen_alias, digest, key, ordered_cells, run_id,
)


def _pinned_json(ref):
    path = Path(ref['path'])
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('noncanonical_continuation_input')
    raw = path.read_bytes()
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('continuation_input_pin_mismatch')
    return json.loads(raw)


class CorpusContinuation:
    def __init__(self, catalog, prototypes, original_jobs, config, reservation, *,
                 family, model_key, task_host, provider_host, config_sha256,
                 repetition_start=1):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', family) or model_key not in MODELS:
            raise ValueError('invalid_continuation_namespace')
        if type(repetition_start) is not int or repetition_start < 1:
            raise ValueError('additional_repetition_required')
        if not task_host or not provider_host or not re.fullmatch('[a-f0-9]{64}', config_sha256):
            raise ValueError('continuation_runtime_binding_required')
        if reservation.get('schema') != 'causes.corpus_allocation_reservation.v1':
            raise ValueError('allocation_reservation_required')
        ids, seeds = reservation['run_ids'], reservation['allocation_seeds']
        if (len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids)
                or len(set(seeds)) != len(seeds)
                or any(type(s) is not int or not 0 < s < SEED_MODULUS for s in seeds)):
            raise ValueError('invalid_allocation_reservation')
        if any(i.startswith(family + '_') for i in ids):
            raise ValueError('continuation_namespace_already_reserved')
        reserved_ids, reserved_seeds = set(ids), set(seeds)
        self.cells = copy.deepcopy(admissible_cells(catalog))
        if not self.cells:
            raise ValueError('nonempty_admissible_catalog_required')
        job_key = lambda job: (job['spec']['task_id'], job['variant']['stimulus_sha256'])
        self.prototypes = {job_key(job): copy.deepcopy(job) for job in prototypes}
        self.originals = {job_key(job): copy.deepcopy(job) for job in original_jobs}
        expected = {key(cell) for cell in self.cells}
        if (len(self.prototypes) != len(prototypes) or len(self.originals) != len(original_jobs)
                or set(self.prototypes) != expected or set(self.originals) != expected):
            raise ValueError('exact_admissible_prototypes_and_lineage_required')
        for cell in self.cells:
            prototype, original = self.prototypes[key(cell)], self.originals[key(cell)]
            alias = chosen_alias(cell)
            variant = prototype['variant']
            if (prototype.get('eligible', True) is not True or variant.get('eligible', True) is not True
                    or variant['id'] != alias['variant_id'] or variant['definition_sha256'] != alias['definition_sha256']
                    or digest({field: variant.get(field) for field in ('request', 'history', 'workspace', 'files', 'env')}) != cell['stimulus_sha256']
                    or digest(original['spec']) != digest(prototype['spec'])
                    or digest(original['variant']) != digest(variant)):
                raise ValueError('canonical_prototype_or_original_changed')
            if original['allocation'].get('source_bank83_run_id', prototype['allocation']['run_id']) != prototype['allocation']['run_id']:
                raise ValueError('original_bank_lineage_mismatch')
            for job in (prototype, original):
                allocation = job['allocation']
                if (allocation.get('continue_to_end') is not False
                        or allocation.get('fixed_study_inference') is not False
                        or allocation.get('primary_memberships') != []
                        or allocation.get('reader_stages') != [] or allocation.get('reader_thresholds') != []
                        or allocation.get('instruments') != ['evaluation_presence']
                        or allocation.get('paired_stage_study') is not False
                        or allocation.get('other_awareness_study') is not False
                        or allocation.get('replacement_for_prior_assignment') is not False):
                    raise ValueError('prefix_only_noninferential_prototype_required')
                if allocation['run_id'] not in reserved_ids or any(allocation[k] not in reserved_seeds for k in SEED_KEYS):
                    raise ValueError('prior_allocation_not_reserved')
        self.family, self.model_key = family, model_key
        self.task_host, self.provider_host = task_host, provider_host
        self.config, self.config_sha256 = copy.deepcopy(config), config_sha256
        self.repetition_start = repetition_start
        self.used_ids, self.used_seeds = reserved_ids, reserved_seeds
        self.binding = {
            'schema': 'causes.corpus_continuation_binding.v1', 'family': family,
            'model_key': model_key, 'task_host': task_host, 'provider_host': provider_host,
            'repetition_start': repetition_start, 'catalog_sha256': digest(catalog),
            'prototype_set_sha256': digest(prototypes), 'original_jobs_sha256': digest(original_jobs),
            'config_object_sha256': digest(config), 'model_config_sha256': config_sha256,
            'reservation_sha256': digest(reservation),
        }
        self.index, self.predecessor = 0, None
        self._pending = None
        self.block_hashes, self.job_hashes, self.job_blocks = {}, {}, {}

    @classmethod
    def from_control(cls, control, prototypes, config):
        if control.get('schema') != 'causes.corpus_continuation_control.v1':
            raise ValueError('continuation_control_schema')
        if digest(_pinned_json(control['config'])) != digest(config):
            raise ValueError('continuation_config_changed')
        if digest(_pinned_json(control['prototypes'])) != digest(prototypes):
            raise ValueError('continuation_prototypes_changed')
        return cls(_pinned_json(control['catalog']), prototypes,
                   _pinned_json(control['original_jobs']), config,
                   _pinned_json(control['reservation']),
                   **{k: control[k] for k in ('family', 'model_key', 'task_host', 'provider_host', 'repetition_start')},
                   config_sha256=control['config']['sha256'])

    def block(self, index):
        if type(index) is not int or index != self.index:
            raise ValueError('continuation_blocks_must_be_verified_in_order')
        if self._pending is not None:
            return copy.deepcopy(self._pending)
        ledger = SeedLedger(self.used_seeds, family=self.family)
        model = self.model_key
        block_seed = ledger.fresh('block', index, model)
        model_seed = ledger.fresh('model', index, model)
        task_seeds = {task: ledger.fresh('task', index, model, task)
                      for task in sorted({cell['task_id'] for cell in self.cells})}
        episode_seeds = {key(cell): ledger.fresh('episode', index, model, *key(cell)) for cell in self.cells}
        jobs, new_ids = [], set()
        for position, ordered in enumerate(ordered_cells(self.cells, model, index, family=self.family)):
            cell = ordered['cell']
            prototype, original = self.prototypes[key(cell)], self.originals[key(cell)]
            identifier = run_id(cell, model, index, family=self.family)
            if identifier in self.used_ids or identifier in new_ids:
                raise ValueError('continuation_run_id_collision')
            new_ids.add(identifier)
            alias = chosen_alias(cell)
            allocation = {
                'run_id': identifier, 'stage': 'corpus_extension', 'corpus_family': self.family,
                'runtime_cohort': self.family, 'block_index': index,
                'block_id': f'{self.family}_b{index:08d}_{model}',
                'repetition': self.repetition_start + index,
                'seed': episode_seeds[key(cell)], 'block_seed': block_seed,
                'task_seed': task_seeds[cell['task_id']], 'model_seed': model_seed,
                'continue_to_end': False, 'instruments': ['evaluation_presence'],
                'reader_stages': [], 'reader_thresholds': [], 'primary_memberships': [],
                'fixed_study_inference': False, 'paired_stage_study': False,
                'other_awareness_study': False, 'replacement_for_prior_assignment': False,
                'selection': 'one_draw_per_admissible_physical_cell_task_interleaved',
                'canonical_variant': alias['variant_id'], 'stimulus_sha256': cell['stimulus_sha256'],
                'canonical_source_id': alias['source_id'],
                'source_variant_bundle_sha256': prototype['allocation']['source_variant_bundle_sha256'],
                'source_version_aliases': copy.deepcopy(cell['source_aliases']),
                'source_bank83_run_id': prototype['allocation']['run_id'],
                'original_corpus_run_id': original['allocation']['run_id'],
                'fixed_order_index': position, 'task_cycle': ordered['cycle'],
                'task_position_in_cycle': ordered['task_position_in_cycle'],
                'task_host': self.task_host, 'provider_host': self.provider_host,
                'model_config_sha256': self.config_sha256,
                'coverage_sha256': self.binding['catalog_sha256'],
                'prototype_sha256': digest(prototype),
            }
            jobs.append({'spec': copy.deepcopy(prototype['spec']),
                         'variant': copy.deepcopy(prototype['variant']), 'allocation': allocation})
        self._pending = {
            'schema': 'causes.corpus_continuation_block.v1', 'block_index': index,
            'block_seed': block_seed, 'model': self.config['model'],
            'binding': self.binding, 'previous_block_sha256': self.predecessor,
            'seed_ledger': ledger.records, 'reserved_seeds_before_sha256': digest(sorted(self.used_seeds)),
            'reserved_seeds_after_sha256': digest(sorted(ledger.used)),
            'balanced_complete_prototype_block': True, 'jobs': jobs,
        }
        return copy.deepcopy(self._pending)

    def accept(self, block):
        """Advance only after exact verification or exclusive block persistence."""
        if digest(block) != digest(self.block(self.index)):
            raise ValueError('continuation_block_or_ledger_mismatch')
        self.block_hashes[self.index] = digest(block)
        self.job_hashes.update({job['allocation']['run_id']: digest(job) for job in block['jobs']})
        self.job_blocks.update({job['allocation']['run_id']: self.index for job in block['jobs']})
        self.used_ids.update(job['allocation']['run_id'] for job in block['jobs'])
        self.used_seeds.update(row['seed'] for row in block['seed_ledger'].values())
        self.predecessor = digest(block)
        self.index += 1
        self._pending = None

    def check_history(self, raw):
        """Reject lost blocks/conflicting claims; return durable, unlogged blocks."""
        if raw and not raw.endswith(b'\n'):
            raise ValueError('incomplete_continuation_journal')
        published, claimed = set(), set()
        for line in raw.splitlines():
            row = json.loads(line)
            if row['event'] == 'extension_block_created':
                index = row['block_index']
                if index in published or self.block_hashes.get(index) != row['block_sha256']:
                    raise ValueError('persisted_continuation_block_missing_or_changed')
                published.add(index)
            elif row['event'] == 'claimed':
                identifier = row['run_id']
                if (identifier in claimed or self.job_hashes.get(identifier) != row['job_sha256']
                        or row.get('config_sha256') != self.binding['config_object_sha256']):
                    raise ValueError('continuation_claim_without_unique_frozen_job')
                if self.job_blocks[identifier] not in published:
                    raise ValueError('continuation_claim_before_block_publication')
                claimed.add(identifier)
        return sorted(set(self.block_hashes) - published)
