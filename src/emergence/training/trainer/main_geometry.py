"""Saved campaign geometry candidate; no native constructor or admission route.

The exact original seed values, ordering expression and arm rotation are retained.
This module does not import the legacy queue, grant authority, choose an acceptance
outcome or claim that an old campaign method/source binding covers new run IDs.
"""
import copy
import hashlib
import json
from emergence.training.trainer.exposure_schedule import SCHEDULE, canonical, digest, lr_at, visits_at

SEEDS = (104729, 130363, 155921, 2546113110, 2526063033, 3714976742, 3514548296, 4272978293, 559270379, 2820813052, 1479892330, 810111749)
MAIN_IDS_SHA = '20a9ffdcd2f2d133ee103f736fed944a0ff0919bee611f1c6cfc53edb13f2b98'
ANCESTOR_QUEUE_SHA = '5927eaf47d1e832c2732d8d53f5f3055e9701e4a1414c5ef93d11588aaad8a2d'
ORDER_RULE = 'sha256(joint-three-source-v1|seed|zero_based_pass|record_id), then record_id'
STATE_SCHEMA = 'candidate-successor1412-main2048-cursor-v1'


def need(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, low=0, high=None):
    need(type(value) is int and value >= low and (high is None or value <= high), 'strict integer')
    return value


def hex64(value):
    need(type(value) is str and len(value) == 64 and set(value) <= set('0123456789abcdef'), 'hash/record ID')
    return value


def frame(record_ids):
    need(type(record_ids) is list and len(record_ids) == 2048, 'exact MAIN count')
    for value in record_ids:
        hex64(value)
    need(len(set(record_ids)) == 2048, 'distinct MAIN records')
    # The original ordered JSONL bytes, not merely a same-size convenient frame.
    raw = b''.join((json.dumps({'candidate_id': rid}, sort_keys=True, separators=(',', ':')) + '\n').encode()
                   for rid in record_ids)
    need(hashlib.sha256(raw).hexdigest() == MAIN_IDS_SHA, 'exact ordered frozen MAIN')
    return {'ordered_frame_sha256': digest(record_ids),
            'frame_sha256': digest(sorted(record_ids)), 'main_ids_sha256': MAIN_IDS_SHA}


def materialize_orders(record_ids):
    identity = frame(record_ids)
    orders = {}
    for seed in SEEDS:
        passes = []
        for p in range(4):
            ids = sorted(record_ids, key=lambda rid: (hashlib.sha256(
                ('joint-three-source-v1|' + str(seed) + '|' + str(p) + '|' + rid).encode('ascii')).digest(), rid))
            passes.append({'pass': p + 1, 'record_ids': ids, 'sha256': digest(ids)})
        orders[str(seed)] = passes
    return {'schema': 'unissued-expanded-main2048-orders-projection-v1', **identity,
            'schedule_sha256': digest(SCHEDULE), 'order_rule': ORDER_RULE,
            'orders': orders, 'quality_acceptance_verified': False,
            'runtime_authority': False}


def validate_orders(orders, record_ids):
    need(canonical(orders) == canonical(materialize_orders(record_ids)), 'exact complete MAIN permutations')


def batch_at(orders, seed, slot):
    need(type(seed) is int and seed in SEEDS, 'paired seed')
    integer(slot, 1, 512)
    need(orders['schema'] == 'unissued-expanded-main2048-orders-projection-v1'
         and orders['main_ids_sha256'] == MAIN_IDS_SHA
         and orders['schedule_sha256'] == digest(SCHEDULE), 'MAIN order identity')
    p, b = divmod(slot - 1, 128)
    order = orders['orders'][str(seed)][p]
    ids = order['record_ids']
    need(type(order['pass']) is int and order['pass'] == p + 1 and len(ids) == 2048
         and len(set(ids)) == 2048 and digest(ids) == order['sha256'], 'exact pass/digest')
    result = {'slot': slot, 'pass': p + 1, 'batch_index': b,
              'global_visit_start': p * 2048 + b * 16,
              'record_ids': ids[b * 16:(b + 1) * 16], 'q': 16,
              'permutation_sha256': order['sha256']}
    global_geometry(result)
    return result


def global_geometry(batch):
    slot = integer(batch['slot'], 1, 512)
    p, b = divmod(slot - 1, 128)
    need(type(batch['pass']) is type(batch['batch_index']) is type(batch['q']) is
         type(batch['global_visit_start']) is int, 'strict geometry types')
    need(batch['pass'] == p + 1 and batch['batch_index'] == b and batch['q'] == 16
         and batch['global_visit_start'] == p * 2048 + b * 16, 'exact geometry/no old tail')
    ids = batch['record_ids']
    need(type(ids) is list and len(ids) == len(set(ids)) == 16, 'exact sixteen distinct records')
    for value in ids:
        hex64(value)
    hex64(batch['permutation_sha256'])
    return 16


def denominator(arm, tokens, q):
    need(type(q) is int and q == 16, 'MAIN global prompt denominator16')
    need(arm in ('sft', 'dpo', 'online_grpo'), 'scientific arm')
    if arm == 'dpo':
        return q
    integer(tokens, 1)
    return tokens * q * (4 if arm == 'online_grpo' else 1)


def rollout_seed(seed, visit, member):
    integer(seed); integer(visit); integer(member, 0, 3)
    return int.from_bytes(hashlib.sha256(('dtrai-rollout-v1|' + str(seed) + '|' + str(visit)
                                         + '|' + str(member)).encode('ascii')).digest()[:8], 'big')


def candidate_plan(ancestor_raw, record_ids):
    need(type(ancestor_raw) is bytes and hashlib.sha256(ancestor_raw).hexdigest() == ANCESTOR_QUEUE_SHA, 'exact scientific ancestor bytes')
    identity = frame(record_ids)
    from pathlib import Path
    raw = (Path(__file__).resolve().parent / 'main2048_inputs/PLAN.json').read_bytes()
    need(len(raw) == PLAN_BYTES and hashlib.sha256(raw).hexdigest() == PLAN_SHA, 'exact source-bound expanded plan')
    plan = json.loads(raw)
    need(plan['data_identity'] == identity and plan['exposure_schedule'] == SCHEDULE and [b['seed'] for b in plan['seed_bundles']] == list(SEEDS), 'exact frame/schedule/seed membership')
    need(len(plan['runs']) == 3 * len(SEEDS) and len(plan['training_segments']) == 3 * len(SEEDS) and all(x['target_completed_update_slots']==128 and x['dependencies']==[] for x in plan['training_segments']), 'complete expanded assignments/barriers')
    return plan


def candidate_cursor(plan, orders, run, completed_slots, optimizer_steps):
    """Exact prospective resume geometry; this is not a saved native state."""
    need(any(canonical(run) == canonical(r) for r in plan['runs']), 'exact candidate run')
    integer(completed_slots, 0, 512); integer(optimizer_steps, 0, completed_slots)
    need(plan['data_identity']['frame_sha256'] == orders['frame_sha256']
         and plan['data_identity']['ordered_frame_sha256'] == orders['ordered_frame_sha256']
         and canonical(plan['exposure_schedule']) == canonical(SCHEDULE), 'plan/order binding')
    seed = run['seed']
    return {'schema': STATE_SCHEMA, 'run_id': run['run_id'], 'seed': seed, 'arm': run['arm'],
            'plan_sha256': digest(plan), 'orders_sha256': digest(orders),
            'schedule_sha256': digest(SCHEDULE), 'data_identity': copy.deepcopy(plan['data_identity']),
            'completed_slots': completed_slots, 'optimizer_steps': optimizer_steps,
            'prompt_visits': visits_at(completed_slots),
            'pass_permutation_sha256': [p['sha256'] for p in orders['orders'][str(seed)]],
            'next_batch': batch_at(orders, seed, completed_slots + 1) if completed_slots < 512 else None,
            'next_lr': lr_at(run['scientific_learning_rate'], completed_slots + 1) if completed_slots < 512 else None,
            'native_state_verified': False}


def validate_candidate_cursor(cursor, plan, orders, run, completed_slots, optimizer_steps):
    need(canonical(cursor) == canonical(candidate_cursor(plan, orders, run, completed_slots, optimizer_steps)),
         'changed or legacy continuation cursor')


def require_runtime_admission(*args, **kwargs):
    raise ValueError('geometry candidate is not a native admission; source/data/runtime composition pending')

PLAN_BYTES = 53309
PLAN_SHA = '49e62109fd907b50c82fbb0a51df37442253d8d298aeb01a4e3ea2526a85aeeb'
