"""Pure prospective identity/order helpers; importing this file writes nothing."""
import collections
import hashlib
import json
import re

FAMILY = 'corpus_bank83_v1'
MODELS = ('mi27', 'tex9')
SEED_KEYS = ('seed', 'block_seed', 'task_seed', 'model_seed')
SEED_MODULUS = 2**31 - 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def key(cell):
    return cell['task_id'], cell['stimulus_sha256']


def admissible_cells(coverage):
    cells = coverage['physical_cells']
    if len({key(cell) for cell in cells}) != len(cells):
        raise ValueError('duplicate_physical_cell')
    for cell in cells:
        if cell['currently_admissible'] != (not cell['hold_ids']):
            raise ValueError('hold_admissibility_mismatch')
    return sorted((cell for cell in cells if cell['currently_admissible']), key=key)


def chosen_alias(cell):
    matches = [alias for alias in cell['source_aliases']
               if alias['source_id'] == cell['earliest_accepted_canonical_source']
               and alias['variant_id'] == cell['earliest_accepted_canonical_variant_id']
               and not alias['alias_of']]
    if len(matches) != 1:
        raise ValueError('canonical_alias_not_unique')
    return matches[0]


def ordered_cells(cells, model, block=0, *, family=FAMILY):
    if model not in MODELS or type(block) is not int or block < 0:
        raise ValueError('invalid_order_domain')
    grouped = collections.defaultdict(list)
    for cell in cells:
        if not cell['currently_admissible'] or cell['hold_ids']:
            raise ValueError('held_cell_in_admission_order')
        grouped[cell['task_id']].append(cell)
    if len({key(cell) for cell in cells}) != len(cells):
        raise ValueError('duplicate_order_cell')
    for task, rows in grouped.items():
        rows.sort(key=lambda cell: (digest([family, 'within_task', block, model, task, cell['stimulus_sha256']]), cell['stimulus_sha256']))
    result = []
    for cycle in range(max((len(rows) for rows in grouped.values()), default=0)):
        tasks = sorted((task for task, rows in grouped.items() if cycle < len(rows)),
                       key=lambda task: (digest([family, 'task_cycle', block, model, cycle, task]), task))
        result.extend({'cell': grouped[task][cycle], 'cycle': cycle, 'task_position_in_cycle': index}
                      for index, task in enumerate(tasks))
    return result


def run_id(cell, model, block=0, *, family=FAMILY):
    alias = chosen_alias(cell)
    task = re.sub('[^A-Za-z0-9_.-]', '_', cell['task_id'])[:48]
    suffix = digest([family, 'episode_identity', block, model, *key(cell)])[:20]
    value = f'{family}_b{block:08d}_{model}_{task}_{alias["variant_id"]}_{suffix}'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,179}', value):
        raise ValueError('invalid_run_id')
    return value


class SeedLedger:
    def __init__(self, prior, *, family=FAMILY):
        self.used = set(prior)
        self.records = {}
        self.family = family
        self.allocated_count = sum(0 < value < SEED_MODULUS for value in self.used)

    def fresh(self, *parts):
        identity = json.dumps([self.family, *parts], sort_keys=True, ensure_ascii=False)
        if identity in self.records:
            return self.records[identity]['seed']
        if self.allocated_count >= SEED_MODULUS - 1:
            raise ValueError('allocation_seed_domain_exhausted')
        counter = 0
        while counter < SEED_MODULUS:
            value = int(digest([self.family, *parts, counter])[:16], 16) % SEED_MODULUS
            if value and value not in self.used:
                break
            counter += 1
        else:
            raise ValueError('allocation_seed_search_exhausted')
        self.used.add(value)
        self.allocated_count += 1
        self.records[identity] = {'seed': value, 'collision_counter': counter}
        return value
