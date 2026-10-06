"""MAIN2048 candidate schedule, separate from every joint1897 continuation."""
import hashlib
import json
import math

SCHEDULE = {
    'schema': 'curated-main2048-exposure-schedule-v1', 'train_records': 2048,
    'global_batch': 16, 'passes': 4, 'slots_per_pass': 128, 'total_slots': 512,
    'tail': 16, 'snapshot_passes': [1, 2, 4], 'snapshot_slots': [128, 256, 512],
    'warmup_slots': 9, 'final_lr_fraction': 0.1,
    'scheduler': 'linear_warmup_cosine_decay_by_update_slot',
    'warmup_choice': 'preserve_absolute_nine_slots_owner_choice_not_percentage',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_schedule(value):
    if canonical(value) != canonical(SCHEDULE):
        raise ValueError('exact MAIN2048 schedule required')
    return value


def visits_at(completed_slots, schedule=None):
    s = validate_schedule(SCHEDULE if schedule is None else schedule)
    if type(completed_slots) is not int or not 0 <= completed_slots <= s['total_slots']:
        raise ValueError('completed slots')
    passes, batches = divmod(completed_slots, s['slots_per_pass'])
    return passes * s['train_records'] + batches * s['global_batch']


def lr_at(peak, slot, schedule=None):
    s = validate_schedule(SCHEDULE if schedule is None else schedule)
    if type(slot) is not int or not 1 <= slot <= s['total_slots']:
        raise ValueError('scheduler slot out of range')
    if type(peak) not in (float, int) or not math.isfinite(peak) or peak <= 0:
        raise ValueError('invalid peak learning rate')
    warmup, total, final = s['warmup_slots'], s['total_slots'], s['final_lr_fraction']
    return peak * (slot / warmup if slot <= warmup else final + (1-final) * 0.5 * (
        1 + math.cos(math.pi * (slot-warmup) / (total-warmup))))
