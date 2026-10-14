"""Join accepted original records to their saved context and protocol evidence.

This is a saved-record adapter, not a response parser or a trajectory verifier.
The caller authenticates the independent reviews and freezes the binding before
use. Missing assignments never acquire invented physical measurements. No model,
request construction, context reconstruction, filesystem inventory or write is
performed. The result does not itself authorize a four-cell analysis.
"""
import copy
import json
import math
from pathlib import PurePosixPath
import re

from causes.campaign_analysis import SELF_LABELS, READER_LABELS
from causes.derived_probe_saved_readout import (
    THRESHOLDS, direct_instrument_protocol, digest, encoded, require)
from shared import evidence_bytes as storage


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _object(items):
    result = {}
    for key, value in items:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _float(value):
    result = float(value)
    require(math.isfinite(result), 'nonfinite_json_float')
    return result


class _Read:
    def __init__(self, document_limit, total_limit):
        for value in (document_limit, total_limit):
            require(type(value) is int and 0 < value <= 512 * 1024**2, 'input_limit')
        self.document_limit, self.total_limit = document_limit, total_limit
        self.refs, self.cache, self.total = {}, {}, 0

    def raw(self, ref):
        storage._pin_shape(ref, self.document_limit)
        path = ref['path']
        require(path not in self.refs or self.refs[path] == ref, 'conflicting_pin')
        if path not in self.refs:
            require(self.total + ref['bytes'] <= self.total_limit, 'total_input_bound')
            self.refs[path] = copy.deepcopy(ref)
            self.total += ref['bytes']
        return storage.read_pinned(ref, limit=self.document_limit)

    def get(self, ref):
        storage._pin_shape(ref, self.document_limit)
        path = ref['path']
        require(path not in self.refs or self.refs[path] == ref, 'conflicting_pin')
        if path not in self.cache:
            self.cache[path] = json.loads(self.raw(ref), object_pairs_hook=_object,
                parse_float=_float,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
        return self.cache[path]

    def accepted(self, descriptor, flag, **joins):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'}, 'descriptor')
        actual, review = self.get(descriptor['actual']), self.get(descriptor['review'])
        require(review.get(flag) is True and review.get('findings') == []
                and review.get('actual') == descriptor['actual'], 'accepted_peer:' + flag)
        require(all(review.get(k) == v for k, v in joins.items()), 'review_join:' + flag)
        return actual, review

    def finish(self):
        for ref in self.refs.values():
            storage.read_pinned(ref, limit=self.document_limit)


def _membership(value):
    require(type(value.get('comparisons')) is list and value['comparisons'], 'comparisons')
    keys, runs = {}, set()
    for comparison in value['comparisons']:
        require(comparison.get('stage') == 'early' and type(comparison.get('members')) is list,
                'early_membership')
        seen = set()
        for member in comparison['members']:
            identity = (member['task_id'], member['draw_key'])
            require(identity not in seen, 'duplicate_member')
            seen.add(identity)
            if member.get('disposition') == 'unsupported':
                require(type(member.get('reason')) is str and member['reason'], 'unsupported_reason')
                continue
            require(member.get('disposition') == 'included'
                    and set(member['arms']) == {'baseline', 'treatment'}, 'member_disposition')
            for arm in member['arms'].values():
                key, run = arm['direct_key'], arm['original_run_id']
                require(type(key) is str and key and type(run) is str and run
                        and arm['input_disposition'] in ('prepared', 'unavailable'), 'member_identity')
                expected = dict(run_id=run, disposition=arm['input_disposition'], view_kind=arm['neutral_view'])
                require(key not in keys or keys[key] == expected, 'shared_membership')
                keys[key] = expected
                runs.add(run)
    return keys, runs


def _protocols(reader, binding):
    p = binding['protocol']
    require(p.get('schema') == 'causes.original_saved_instrument_binding.v1', 'protocol_binding_schema')
    assembly = reader.get(p['assembly_binding'])
    require(type(assembly.get('model')) is str and assembly['model']
            and assembly['model_config'] == p['direct_config']
            and p['runner_source'] in assembly['required_pins']
            and assembly['reader_config'] == p['reader_config'], 'assembly_protocol_sources')
    config = reader.get(p['direct_config'])
    reader.raw(p['runner_source'])
    questions = {q['id']: q for q in config['questions']}
    require(len(questions) == len(config['questions']), 'duplicate_question')
    direct = {name: direct_instrument_protocol(config, p['runner_source'], questions['evaluation_' + name])
              for name in THRESHOLDS}
    require(direct == p['self'], 'direct_protocol_identity')
    reader_config = reader.get(p['reader_config'])
    reader.raw(p['reader_source'])
    require(set(p['reader_prompts']) == {'rubric', *THRESHOLDS}, 'reader_prompt_roster')
    for ref in p['reader_prompts'].values():
        reader.raw(ref)
    require(set(p['reader']) == set(THRESHOLDS), 'reader_protocol_roster')
    for name, semantic in p['reader'].items():
        require(type(semantic) is dict and set(semantic) == {'schema', 'reader_source_sha256',
            'config_sha256', 'rubric_sha256', 'threshold_sha256', 'schema_sha256', 'instrument',
            'phase', 'awareness_type'}, 'reader_semantic_shape')
        require(semantic['schema'] == 'causes.saved_reader_instrument_protocol.v1'
                and semantic['instrument'] == name and semantic['phase'] == 'prefix'
                and semantic['awareness_type'] == 'evaluation'
                and all(_hash(v) for k, v in semantic.items() if k.endswith('_sha256')),
                'reader_semantic_scope')
        require(semantic['reader_source_sha256'] == p['reader_source']['sha256']
                and semantic['config_sha256'] == digest(reader_config)
                and semantic['rubric_sha256'] == p['reader_prompts']['rubric']['sha256']
                and semantic['threshold_sha256'] == p['reader_prompts'][name]['sha256'],
                'reader_semantic_identity')
    return config, direct, p['reader'], assembly['model']


def _proof_context(reader, ref, selector, original, variant, *, run_id, allocation, config):
    proof = reader.get(ref)
    # Saved single-arm preparations wrap the proof in a provenance envelope.
    # Its logical selector remains relative to the authenticated inner proof.
    if proof.get('schema') is None and 'proof' in proof:
        require(set(proof) in (
                {'arm', 'baseline_model', 'construction', 'original_files',
                 'proof', 'run_id', 'sample_adoption', 'single_arm_adoption', 'summary'},
                {'arm', 'baseline_model', 'candidate', 'construction', 'membership',
                 'original_files', 'proof', 'run_id', 'single_arm_adoption', 'summary'}),
                'single_proof_envelope_shape')
        require(proof['run_id'] == run_id
                and proof['arm'] == ('baseline' if variant == 'B00L00' else 'treatment'),
                'single_proof_envelope_identity')
        files = proof['original_files']
        require(type(files) is dict and files.get('allocation.json') == allocation
                and files.get('model.json') == config, 'single_proof_envelope_sources')
        context = files.get('early_context.json')
        storage._pin_shape(context, reader.document_limit)
        require(context['path'] == original['path']
                and context['sha256'] == original['bytes_sha256'], 'single_proof_envelope_context')
        require(type(proof['proof']) is dict
                and proof['proof'].get('schema') == 'causes.neutral_summary_single_arm_derivation.v1',
                'single_proof_envelope_schema')
        proof = proof['proof']
    if proof.get('schema') == 'causes.neutral_summary_pair_derivation.v1':
        require(selector in (['arms', 'baseline', 'contexts', 'messages'],
                             ['arms', 'treatment', 'contexts', 'messages']), 'paired_proof_selector')
        require(selector[1] == ('baseline' if variant == 'B00L00' else 'treatment'), 'proof_arm')
    else:
        require(proof.get('schema') == 'causes.neutral_summary_single_arm_derivation.v1'
                and selector == ['derivation', 'contexts', 'messages']
                and proof.get('counterpart_context_supplied') is False
                and proof.get('arm') == ('baseline' if variant == 'B00L00' else 'treatment'),
                'single_proof_selector')
    reached = proof
    for key in selector:
        reached = reached[key]
    require(reached.get('exact_inverse_verified') is True
            and _hash(original['context_sha256'])
            and reached.get('original_sha256') == original['context_sha256'], 'original_context_proof')


def _contexts(reader, inventory, keys, natives, config):
    pins = inventory['pins']
    require(type(pins) is list, 'inventory_pins')
    paths = set()
    for pin in pins:
        storage._pin_shape(pin, reader.document_limit)
        require(pin['path'] not in paths, 'duplicate_inventory_pin')
        paths.add(pin['path'])
    def pin(index):
        require(type(index) is int and 0 <= index < len(pins), 'inventory_pin_index')
        return pins[index]
    rows, by_run = {}, {}
    for entry in inventory['contexts']:
        key, run = entry['measurement_key'], entry['run_id']
        require(key not in rows and key in keys and run in natives
                and {k: entry[k] for k in ('run_id', 'disposition', 'view_kind')} == keys[key],
                'inventory_context_membership')
        native = natives[run]
        original = native['context_sources'].get('early')
        if entry['disposition'] == 'unavailable':
            require(original is None and type(entry.get('reason')) is str and entry['reason']
                    and not any(k in entry for k in ('proof', 'allocation', 'config', 'original_context')),
                    'unavailable_context_identity')
        else:
            require(type(original) is dict and original == entry['original_context']
                    and _hash(original.get('bytes_sha256')), 'native_context_identity')
            allocation = reader.get(pin(entry['allocation']))
            require(allocation == native['allocation'] and allocation['run_id'] == run
                    and allocation['canonical_variant'] == native['variant_id'], 'native_allocation_identity')
            original_config = reader.get(pin(entry['config']))
            require({k: v for k, v in original_config.items() if k != 'endpoint'} ==
                    {k: v for k, v in config.items() if k != 'endpoint'}, 'origin_protocol_changed')
            _proof_context(reader, pin(entry['proof']), entry['proof_selector'], original, native['variant_id'],
                           run_id=run, allocation=pin(entry['allocation']), config=pin(entry['config']))
        require(run not in by_run or by_run[run] == original, 'conflicting_original_context')
        by_run[run] = original
        rows[key] = copy.deepcopy(entry)
    require(set(rows) == set(keys), 'complete_context_membership')
    return pin, by_run


def _reader_contexts(reader, inventory, pin, natives, original_contexts, protocol):
    rows = {}
    for entry in inventory['readers']:
        run = entry['run_id']
        require(run in natives and run not in rows, 'reader_inventory_membership')
        native = natives[run]['reader'].get('early')
        result_pin, context_pin = pin(entry['result']), pin(entry['context'])
        require(type(native) is dict and native['path'] == result_pin['path']
                and native['result_sha256'] == result_pin['sha256']
                and PurePosixPath(result_pin['path']).parent / 'context.json' == PurePosixPath(context_pin['path']),
                'reader_physical_identity')
        require(entry['context_source']['bytes'] == context_pin['bytes']
                and entry['context_source']['sha256'] == context_pin['sha256'], 'reader_copy_identity')
        complete = reader.get(entry['complete'])
        require(complete.get('schema') == 'causes.reader_artifact_freeze_complete.v1'
                and complete['provenance']['namespace'] == entry['namespace'], 'reader_namespace')
        native_pins = complete['provenance']['reader_pins']
        require(native_pins['reader_source_sha256'] == protocol['reader_source']['sha256']
                and native_pins['reader_config_sha256'] == protocol['reader_config']['sha256']
                and native_pins['prompts'] == {k: v['sha256'] for k, v in protocol['reader_prompts'].items()},
                'native_reader_protocol')
        context, result = reader.get(context_pin), reader.get(result_pin)
        original = original_contexts[run]
        require(original is not None and entry['native_context_sha256'] == original['context_sha256']
                == context.get('context_sha256') == result.get('context_sha256'), 'reader_context_domain')
        require(type(context.get('messages')) is list and context['observation'].get('phase') == 'prefix'
                and result.get('schema') == 'caus.reader_context.v1'
                and result.get('observation') == context['observation']
                and result.get('independent_instrument_calls') is True
                and result.get('threshold_results_inferred_from_other_calls') is False
                and set(result['instruments']) == set(THRESHOLDS), 'reader_saved_shape')
        for name in THRESHOLDS:
            saved, compact = result['instruments'][name], native['instruments'][name]
            require(saved.get('instrument') == name and saved.get('context_sha256') == original['context_sha256']
                    and compact['status'] == saved['status'], 'reader_instrument_identity')
            label = ((saved.get('judgments') or {}).get('evaluation') or {}).get('admissible_label')
            admitted = label if saved['status'] in ('validated', 'validated_partial_observation') and label in READER_LABELS else None
            require(compact['labels'].get('evaluation') == admitted, 'accepted_reader_label_join')
        rows[run] = dict(context_sha256=digest(context), native_context_sha256=original['context_sha256'],
                         result_pin=result_pin, context_pin=context_pin, result=result, entry=entry)
    expected = {run for run, n in natives.items() if n['reader'].get('early') is not None}
    require(set(rows) == expected, 'complete_reader_inventory')
    return rows


def resolve_saved_originals(subset, membership, binding, *, maximum_document_bytes=32*1024**2,
                            maximum_total_bytes=64*1024**2, maximum_result_bytes=12*1024**2):
    """Return real measurements, the full assignment ledger and literal provenance.

    All three arguments are {actual, review} descriptors. The binding review
    accepts exact subset/membership/inventory/protocol joins; it must be obtained
    independently before any real input is resolved. The inventory contains the
    explicit ordered pin table and context/reader rows, never inferred paths.
    Protocol schema hashes and the assembly binding's original campaign are
    authenticated by that review and native source custody; this adapter does
    not rebuild requests or output schemas.

    ``not_assigned`` and assigned-but-absent records have null measurement IDs.
    IDs for present self records identify snapshot/summary/instrument records,
    not HTTP requests. Reader IDs identify the accepted result file/instrument.
    Self contexts use the native message-array digest; reader contexts use the
    canonical saved prepared object, retaining the native digest separately.
    Unique input bytes exclude the final full byte recheck (additional read I/O).
    """
    require(type(maximum_result_bytes) is int and 0 < maximum_result_bytes <= 64*1024**2, 'output_limit')
    rd = _Read(maximum_document_bytes, maximum_total_bytes)
    selected_actual, selected_peer = rd.accepted(subset, 'accepted_saved_original_fixed_trajectory_subset')
    selected = rd.get(selected_actual['data'])
    require(selected_peer.get('data') == selected_actual['data']
            and selected.get('schema') == 'causes.saved_original_fixed_trajectory_subset.v1'
            and selected.get('native_rows_unchanged') is True, 'accepted_native_subset')
    members, _ = rd.accepted(membership, 'accepted_frozen_neutral_matched_membership')
    bindings, binding_peer = rd.accepted(binding, 'accepted_original_saved_registry_bindings',
                                        subset=subset, membership=membership)
    require(bindings.get('schema') == 'causes.original_saved_registry_binding.v1'
            and bindings.get('subset') == subset and bindings.get('membership') == membership,
            'registry_binding')
    inventory = rd.get(bindings['inventory'])
    require(binding_peer.get('inventory') == bindings['inventory']
            and inventory.get('schema') == 'causes.original_saved_registry_reached_metadata.v1'
            and inventory['selected'] == selected_actual['data']
            and inventory['membership'] == membership['actual'], 'inventory_binding')
    keys, runs = _membership(members)
    require(type(selected['selected']) is list, 'native_rows')
    natives, selected_rows = {}, {}
    for row in selected['selected']:
        run, native = row['run_id'], row['native']
        require(type(run) is str and run in runs and run not in natives
                and native['run_id'] == run and native['allocation']['run_id'] == run
                and native['allocation']['canonical_variant'] == native['variant_id'], 'native_run_identity')
        require(_hash(native['summary_sha256']) and type(native['source']) is str
                and native['source'].startswith('/'), 'native_snapshot_identity')
        natives[run], selected_rows[run] = native, row
    require(set(natives) == runs, 'complete_native_membership')
    config, self_protocols, reader_protocols, namespace = _protocols(rd, bindings)
    require(all(n['model'] == namespace for n in natives.values()), 'native_model_namespace')
    pin, original_contexts = _contexts(rd, inventory, keys, natives, config)
    readers = _reader_contexts(rd, inventory, pin, natives, original_contexts, bindings['protocol'])
    measurements, assignments, provenance = [], [], []
    identities = set()
    expected_slots = {(method, name) for method in ('self', 'reader') for name in THRESHOLDS}
    for run, native in natives.items():
        roster = selected_rows[run]['assignments']
        require(type(roster) is list and len(roster) == 6
                and {(x['method'], x['threshold']) for x in roster} == expected_slots,
                'complete_assignment_roster')
        for slot in roster:
            method, name = slot['method'], slot['threshold']
            require(slot['disposition'] in ('assigned', 'not_assigned'), 'assignment_disposition')
            instrument = 'evaluation_' + name
            assigned = (instrument in native['allocation']['instruments'] if method == 'self'
                        else 'early' in native['allocation']['reader_stages']
                        and name in native['allocation']['reader_thresholds'])
            require(assigned == (slot['disposition'] == 'assigned'), 'native_assignment_join')
            record = (native['self'].get('early', {}).get(instrument) if method == 'self'
                      else native['reader'].get('early', {}).get('instruments', {}).get(name))
            if not assigned:
                require(record is None, 'unassigned_has_measurement')
            ledger = dict(original_run_id=run, stage='early', method=method, threshold=name,
                          disposition=slot['disposition'], measurement_id=None,
                          record_state='not_assigned' if not assigned else 'absent_instance')
            if record is not None:
                context = original_contexts[run]
                native_hash = context['context_sha256'] if context else None
                if method == 'self':
                    label = record['label']
                    require(label is None or label in SELF_LABELS, 'native_self_label')
                    require(label is None or record['technical_status'] == 'complete', 'native_self_status')
                    require(record.get('context_sha256') in (None, native_hash)
                            and (label is None or record.get('context_sha256') == native_hash), 'self_context_identity')
                    physical = dict(kind='native_snapshot_measurement', source=native['source'],
                                    summary_sha256=native['summary_sha256'], stage='early', instrument=instrument)
                    context_hash, semantic = native_hash, self_protocols[name]
                    detail = copy.deepcopy(record)
                else:
                    saved = readers[run]
                    label = record['labels'].get('evaluation')
                    require(label is None or label in READER_LABELS, 'native_reader_label')
                    physical = dict(kind='native_reader_result', result=saved['result_pin'], stage='early', instrument=name)
                    context_hash, semantic = saved['context_sha256'], reader_protocols[name]
                    detail = copy.deepcopy(saved['result']['instruments'][name])
                require(context_hash is not None or label is None, 'observed_without_context')
                identity = 'original_record_' + digest(physical)
                require(identity not in identities, 'duplicate_physical_record')
                identities.add(identity)
                status = 'observed' if label is not None else ('unavailable' if context_hash is None else 'technical_missing')
                measurements.append(dict(id=identity, original_run_id=run, stage='early', method=method,
                    awareness_type='evaluation', threshold=name, context_sha256=context_hash,
                    protocol_sha256=digest(semantic), label=label, status=status,
                    reason=None if label is not None else ('original_context_unavailable' if context_hash is None else 'native_technical_missing')))
                ledger.update(measurement_id=identity, record_state='present')
                provenance.append(dict(id=identity, physical=physical, native_context_sha256=native_hash,
                    selected_row=selected_rows[run]['provenance'], native_record=detail,
                    native_compact_record=copy.deepcopy(record)))
            assignments.append(ledger)
    counts = {name: sum(r['disposition'] == name for r in assignments) for name in ('assigned', 'not_assigned')}
    require(counts == selected['assignment_counts'], 'assignment_counts')
    result = dict(schema='causes.saved_original_measurement_registry.v1', subset=copy.deepcopy(subset),
        membership=copy.deepcopy(membership), binding=copy.deepcopy(binding),
        measurements=measurements, assignments=assignments, provenance=provenance,
        comparisons=copy.deepcopy(members['comparisons']),
        protocols=dict(self=self_protocols, reader=reader_protocols),
        counts=dict(**counts, present=len(measurements), absent_instances=sum(r['record_state'] == 'absent_instance' for r in assignments)),
        input_pins=list(rd.refs.values()), input_unique_bytes=rd.total,
        input_finish_recheck_bytes=rd.total, native_parser_calls=0, model_calls=0,
        inference_released=False, four_cell_analysis_authorized=False)
    require(len(encoded(result)) <= maximum_result_bytes, 'serialized_output_bound')
    rd.finish()
    return result
