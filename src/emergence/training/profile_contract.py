"""Prospective stable method / batch provenance split. No admission or execution.

Callers must authenticate the supplied documents with the selected source/data
and collection validators. This pure module neither substitutes for those
validators nor permits a finite engineering entry to run a campaign.
"""
import copy
import hashlib
import json
import math

PROFILE_SCHEMA='vllm-campaign-algorithm-profile-v1'
METHOD_SCHEMA='vllm-campaign-per-run-method-v1'
BATCH_SCHEMA='vllm-campaign-batch-provenance-v1'
PROFILE_KEYS={'schema','profile_id','sources','artifacts','numerics','objective','schedule_sha256','dataset_identity_sha256'}
SOURCE_KEYS={'learner','collector','policy_loader','objective','update','dense_scorer'}
ARTIFACT_KEYS={'model_manifest','tokenizer','adapter_mapping','engine_inventory'}
DYNAMIC_KEYS={'policy','policy_version','plan','collection','batch','slot','completed_slot','producer',
              'output_root','claim_root','wall_end','boot_end','attempt_id','process','gpu_uuid'}

def need(ok,message):
    if not ok:raise ValueError(message)

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()

def sha(value):
    return type(value) is str and len(value)==64 and set(value)<=set('0123456789abcdef')

def pin(ref):
    need(type(ref) is dict and set(ref)=={'path','bytes','sha256'} and type(ref['path']) is str
         and ref['path'].startswith('/') and type(ref['bytes']) is int and ref['bytes']>0
         and sha(ref['sha256']),'exact immutable reference shape')
    return copy.deepcopy(ref)

def fixed(value):
    """No current-policy, batch, process, output or clock belongs to algorithm."""
    if type(value) is dict:
        need(not (set(value)&DYNAMIC_KEYS),'dynamic provenance inside stable algorithm')
        for child in value.values():fixed(child)
    elif type(value) is list:
        for child in value:fixed(child)
    elif type(value) is float:need(math.isfinite(value),'finite fixed numerical value')
    else:need(value is None or type(value) in (str,int,bool),'JSON-only fixed profile')

def method_identity(profile,trajectory):
    """Stable per run; changing a slot/collection cannot change this function."""
    need(type(profile) is dict and set(profile)==PROFILE_KEYS and profile['schema']==PROFILE_SCHEMA,
         'complete explicitly distinct vLLM profile')
    need(profile['profile_id'] == EXPECTED_PROFILE_ID,'declared algorithm name')
    need(set(profile['sources'])==SOURCE_KEYS and set(profile['artifacts'])==ARTIFACT_KEYS,'complete fixed source/artifact roles')
    for ref in [*profile['sources'].values(),*profile['artifacts'].values()]:pin(ref)
    need(sha(profile['schedule_sha256']) and sha(profile['dataset_identity_sha256']),'fixed schedule/data')
    fixed(profile)
    need(type(trajectory) is dict and set(trajectory)=={'run_id','seed','initial_adapter_id','adapter_init_seed',
         'initial_adapter_digest','reference_identity'},'complete stable trajectory identity')
    need(type(trajectory['seed']) is int and trajectory['seed'] in TRAJECTORIES
         and all(trajectory[k] == v for k,v in TRAJECTORIES[trajectory['seed']].items())
         and type(trajectory['adapter_init_seed']) is int and trajectory['adapter_init_seed']>=0
         and sha(trajectory['initial_adapter_digest']) and type(trajectory['reference_identity']) is dict,
         'actual declared GRPO trajectory/common identity')
    fixed(trajectory)
    return dict(schema=METHOD_SCHEMA,algorithm=copy.deepcopy(profile),trajectory=copy.deepcopy(trajectory))

def batch_provenance(method,*,plan,plan_ref,policy,policy_ref,collection,collection_ref,export_ref):
    """Bind authenticated batch documents separately, without creating authority."""
    need(method==method_identity(method['algorithm'],method['trajectory']),'canonical stable method')
    run=method['trajectory']['run_id'];seed=method['trajectory']['seed'];slot=plan['slot']
    need(type(slot) is int and 1<=slot<=512 and plan['run_id']==policy['run_id']==run
         and plan['seed']==policy['seed']==seed and policy['completed_slot']==slot-1,
         'next precommitted slot of the same run')
    need(plan['policy']==policy_ref and plan['policy_version']==policy['policy_version']
         and sha(policy['policy_version']),'same frozen FP32 policy')
    batch=plan['batch'];requests=plan['requests']
    need(batch['slot']==slot and batch['q']==16 and len(batch['record_ids'])==16
         and len(set(batch['record_ids']))==16 and len(requests)==64,'whole original q16/64 batch')
    ids=[r['sample_id'] for r in requests]
    need(len(set(ids))==64 and all(r['policy_version']==policy['policy_version'] for r in requests),
         'unique requests frozen to one policy')
    need(collection['phase']=='ordered64' and collection['complete_original_q16_batch'] is True
         and collection['sample_count']==64 and collection['slot']==slot
         and collection['policy']==policy_ref and collection['plan']==plan_ref and collection['export']==export_ref
         and collection['policy_version']==policy['policy_version'] and collection['ordered_sample_ids']==ids,
         'one complete authenticated collection for the exact plan/export')
    return dict(schema=BATCH_SCHEMA,method_sha256=digest(method),run_id=run,slot=slot,
                policy_version=policy['policy_version'],policy=pin(policy_ref),plan=pin(plan_ref),
                export=pin(export_ref),collection=pin(collection_ref),batch_sha256=digest(batch),
                ordered_sample_ids=copy.deepcopy(ids))

def checkpoint_method_fields(method,pending):
    """Only the changed method/plan fields; full production state remains required."""
    need(method==method_identity(method['algorithm'],method['trajectory']),'canonical stable method')
    if pending is None:
        return dict(grpo_method_identity=copy.deepcopy(method),generation_request_plan=None)
    provenance=pending['batch_provenance']
    need(provenance['schema']==BATCH_SCHEMA and provenance['method_sha256']==digest(method)
         and provenance['run_id']==method['trajectory']['run_id']
         and pending['grpo_method_identity']==method and pending['run_id']==provenance['run_id']
         and pending['policy_version']==provenance['policy_version']
         and pending['batch']['slot']==provenance['slot']
         and digest(pending['batch'])==provenance['batch_sha256'],'pending stable-method/provenance join')
    # This pin points to the NEW collector plan schema. It is never a fabricated
    # old group4 binding and must be checked by the explicit new-profile branch.
    need(pending['generation_request_plan']==provenance['plan'],'actual new-profile pending plan pin')
    return dict(grpo_method_identity=copy.deepcopy(method),generation_request_plan=pin(provenance['plan']))

def validate_method_history(meta,ledger,pending,method):
    """Supplement the retained full-state validator, never replace it."""
    fields=checkpoint_method_fields(method,pending)
    need(all(meta[k]==v for k,v in fields.items()),'checkpoint method/pending-plan identity')
    need(all(item['grpo_method_identity']==method for item in ledger),'stable method across every committed slot')
    # Adapter, AdamW membership/moments/steps, ledger policy chain, scheduler,
    # exact RNG, data/order, full pending probabilities and custody remain the
    # unchanged production restore validator's responsibility.
    return True

TRAJECTORIES = {104729: {'run_id': 'dtrai-corrected1887-main2048-warmup9-v1-seed104729-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-initial-v1-seed104729', 'adapter_init_seed': 2067258149}, 130363: {'run_id': 'dtrai-corrected1887-main2048-warmup9-v1-seed130363-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-initial-v1-seed130363', 'adapter_init_seed': 539897453}, 155921: {'run_id': 'dtrai-corrected1887-main2048-warmup9-v1-seed155921-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-initial-v1-seed155921', 'adapter_init_seed': 4218720006}, 2546113110: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed2546113110-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed2546113110', 'adapter_init_seed': 986562119}, 2526063033: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed2526063033-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed2526063033', 'adapter_init_seed': 4176431656}, 3714976742: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed3714976742-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed3714976742', 'adapter_init_seed': 3917537971}, 3514548296: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed3514548296-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed3514548296', 'adapter_init_seed': 575870547}, 4272978293: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed4272978293-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed4272978293', 'adapter_init_seed': 1690184883}, 559270379: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed559270379-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed559270379', 'adapter_init_seed': 1135370504}, 2820813052: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed2820813052-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed2820813052', 'adapter_init_seed': 511981958}, 1479892330: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed1479892330-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed1479892330', 'adapter_init_seed': 654299992}, 810111749: {'run_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-seed810111749-online_grpo_token_is_v1', 'initial_adapter_id': 'dtrai-main2048-v5-paired-extension-20261005-v1-initial-seed810111749', 'adapter_init_seed': 649083167}}
EXPECTED_PROFILE_ID = 'vllm-campaign-bf16-rollout-native-race-fp32-strict-ce-q16-corrected-terminal-region-12c505c76fa4-v1-paired-n12-one-pass128'
