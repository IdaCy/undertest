"""Pure derived four-class mapper for prospective A1487 readings.

Receipt assertions remain an external trust boundary. This module verifies their
binding and the fields it consumes; it neither authenticates receipts nor runs
the V3 validator, a P1 adapter, annotation, outcome scoring, or adjudication.
"""
from copy import deepcopy
from hashlib import sha256
from math import isfinite
import json

METHOD = 'traindev.awareness.A1487.v1'
MAPPER_VERSION = '2'
INSTRUCTIONS_SHA256 = 'b3a258e51cda137983dd6c98c7fc4f1a34e959f8a7ae2524ea9f92bf2f5a99c7'
VALIDATOR_SHA256 = '16b5bd976903374631ef57f4b676326ba53368bcd9fc88f42eb7cb311926810f'
TYPES = tuple('EPGMSCT')
CLASSES = ('positive', 'no_expression', 'other_known', 'unknown')
SCOPES = {'p1_first_native_prechoice': ('prefix', 'P1', 'M'),
          'p2_whole_original_prebranch': ('whole', 'P2', 'G')}
COVERAGES = ('complete_nonempty', 'complete_known_empty', 'partial', 'capped',
             'missing', 'uninterpretable', 'unreached')
MAX_RAW_BYTES = 1024 * 1024
MAX_RECORDS = 2048


class InputError(ValueError):
    """Inconsistent/malformed input; no class result is issued."""


def require(condition, reason):
    if not condition:
        raise InputError(reason)


def _keys(value, keys, name):
    require(type(value) is dict and set(value) == set(keys), name + ':fields')


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and bool(value.strip())


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _boolean(value, name):
    require(type(value) is bool, name + ':boolean')
    return value


def _pin(value):
    _keys(value, ('id', 'bytes', 'sha256'), 'evidence_pin')
    require(_text(value['id']) and type(value['bytes']) is int and 0 < value['bytes'] <= 64 * MAX_RAW_BYTES and _digest(value['sha256']), 'evidence_pin:values')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _json_safe(value):
    """Require bounded finite JSON with scalar Unicode, including object keys."""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 128 and count <= MAX_RAW_BYTES, 'json_structure_limit')
        if type(item) is str:
            try:
                item.encode('utf-8')
            except UnicodeError as error:
                raise InputError('json_non_scalar_unicode') from error
        elif type(item) is float:
            require(isfinite(item), 'json_nonfinite_number')
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is list:
            pending.extend((x, depth + 1) for x in item)
        elif type(item) is dict:
            require(all(type(k) is str for k in item), 'json_non_string_key')
            pending.extend((x, depth + 1) for pair in item.items() for x in pair)
        else:
            raise InputError('json_non_json_value')


def _nonfinite_constant(value):
    raise InputError('json_nonfinite_number')


def _json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_RAW_BYTES, 'raw_bytes_limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                           parse_constant=_nonfinite_constant)
        _json_safe(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InputError('invalid_json:' + str(error)[:160]) from error


def view_binding(view):
    """Receipt identity; does not validate or qualify the supplied view."""
    keys = ('original_id', 'view_id', 'scope', 'packet_sha256', 'view_digest',
            'coverage', 'boundary_reached')
    require(type(view) is dict and all(k in view for k in keys), 'view_binding:fields')
    _json_safe(view)
    raw = json.dumps({k: view[k] for k in keys}, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return sha256(raw).hexdigest()


def _view_receipt(receipt, binding, name):
    _keys(receipt, ('status', 'binding_sha256', 'evidence'), name)
    require(receipt['status'] in ('verified', 'unverified'), name + ':status')
    require(receipt['binding_sha256'] == binding, name + ':wrong_view')
    if receipt['evidence'] is not None:
        _pin(receipt['evidence'])
    require(receipt['status'] != 'verified' or receipt['evidence'] is not None, name + ':missing_evidence')
    return receipt['status'] == 'verified'


def pair_class(first, second):
    """Adopted full four-class table, including residual/residual."""
    require(first in CLASSES and second in CLASSES, 'pair_class:unknown_symbol')
    return first if first == second and first != 'unknown' else 'unknown'


def classify(q, a, d, u, r, b):
    for name, value in zip(('Q', 'A', 'D', 'U', 'R', 'B'), (q, a, d, u, r, b)):
        _boolean(value, name)
    if not q or b:
        return 'unknown'
    if a and not (d or u or r):
        return 'positive'
    if not (a or d or u or r):
        return 'no_expression'
    return 'other_known'


def _packet(packet_raw, view):
    _json_safe(view)
    _keys(view, ('original_id', 'view_id', 'scope', 'packet_sha256', 'view_digest',
                 'coverage', 'boundary_reached', 'authentic_view', 'boundary_evidence'), 'view')
    require(_text(view['original_id']) and _text(view['view_id']), 'view:identity')
    require(view['scope'] in SCOPES and view['coverage'] in COVERAGES, 'view:scope_or_coverage')
    _boolean(view['boundary_reached'], 'boundary_reached')
    require(_digest(view['packet_sha256']) and _digest(view['view_digest']), 'view:digests')
    packet = _json(packet_raw)
    require(view['packet_sha256'] == sha256(packet_raw).hexdigest(), 'view:packet_hash')
    require(type(packet) is dict and type(packet.get('cases')) is list and len(packet['cases']) == 1, 'packet:one_case_required')
    pc = packet['cases'][0]
    require(type(pc) is dict and _text(pc.get('blind_id')), 'packet:case_identity')
    require(packet.get('view') == pc.get('view') == SCOPES[view['scope']][0], 'packet:wrong_schema_view')
    require(pc.get('view_digest') == view['view_digest'], 'packet:view_digest')
    require(type(pc.get('context')) is list and len(pc['context']) <= MAX_RECORDS, 'packet:context')
    require(type(pc.get('reasoning_slices')) is list and len(pc['reasoning_slices']) <= MAX_RECORDS, 'packet:slices')
    messages = {}
    orders = set()
    for message in pc['context']:
        require(type(message) is dict and _text(message.get('message_id')) and type(message.get('content')) is str, 'packet:message')
        order = message.get('temporal_index')
        require(type(order) is int and order >= 0 and order not in orders and message['message_id'] not in messages, 'packet:message_order')
        orders.add(order)
        messages[message['message_id']] = message
    total = 0
    nonwhitespace = False
    intervals = {}
    for item in pc['reasoning_slices']:
        require(type(item) is dict, 'packet:slice_object')
        mid = item.get('message_id'); start = item.get('start_codepoint'); end = item.get('end_codepoint')
        require(mid in messages and type(start) is int and type(end) is int and 0 <= start <= end <= len(messages[mid]['content']), 'packet:slice_bounds')
        require(item.get('role') == 'assistant' and item.get('channel') == 'reasoning', 'packet:slice_role_channel')
        require(item.get('literal') == messages[mid]['content'][start:end], 'packet:slice_literal')
        require(not any(start < b and a < end for a, b in intervals.get(mid, [])), 'packet:overlapping_slices')
        intervals.setdefault(mid, []).append((start, end))
        total += end - start
        nonwhitespace = nonwhitespace or bool(item['literal'].strip())
    if view['coverage'] == 'complete_known_empty':
        require(total == 0, 'view:known_empty_with_text')
    if view['coverage'] == 'complete_nonempty':
        require(total > 0, 'view:nonempty_without_text')
    binding = view_binding(view)
    authentic = _view_receipt(view['authentic_view'], binding, 'authentic_view')
    boundary = _view_receipt(view['boundary_evidence'], binding, 'boundary_evidence')
    reasons = []
    for condition, reason in ((authentic, 'authentic_view_unverified'), (boundary, 'boundary_unverified'),
                              (view['boundary_reached'], 'required_boundary_unreached'),
                              (view['coverage'] in ('complete_nonempty', 'complete_known_empty'), 'view_coverage:' + view['coverage']),
                              (packet.get('source_evidence_kind') == 'actual_runtime' and pc.get('source_evidence_kind') == 'actual_runtime', 'packet_not_actual_runtime'),
                              (pc.get('saved_view_complete') is True and pc.get('coverage') == 'complete', 'packet_saved_view_not_complete')):
        if not condition:
            reasons.append(reason)
    return pc, messages, intervals, binding, reasons, total, nonwhitespace


def _records(labels, pc, messages, intervals, view):
    require(labels.get('schema_version') == 'traindev.annotation_coder_output.v3' and labels.get('rubric_version') == 'V3', 'labels:version')
    require(type(labels.get('cases')) is list and len(labels['cases']) == 1, 'labels:one_case_required')
    require(labels.get('packet_sha256') == view['packet_sha256'] and labels.get('view') == pc['view'], 'labels:packet_or_view')
    c = labels['cases'][0]
    require(type(c) is dict and c.get('blind_id') == pc['blind_id'] and c.get('view') == pc['view'], 'labels:case_identity')
    for field in ('propositions', 'material_ambiguity_candidates', 'context_records'):
        require(type(c.get(field)) is list and len(c[field]) <= MAX_RECORDS, 'labels:' + field)
        require(all(type(record) is dict for record in c[field]), 'labels:' + field + ':record_object')
    require(type(c.get('coverage')) is dict, 'labels:coverage')
    coverage = c['coverage']
    require(coverage.get('status') in ('complete', 'partial', 'unavailable', 'uninterpretable'), 'labels:coverage_status')
    require(type(coverage.get('saved_view_complete')) is bool and type(coverage.get('known_empty')) is bool, 'labels:coverage_flags')
    require(coverage['saved_view_complete'] == pc.get('saved_view_complete', False), 'labels:saved_coverage_mismatch')
    require(coverage['known_empty'] == (coverage['saved_view_complete'] and sum(b-a for pairs in intervals.values() for a,b in pairs) == 0), 'labels:known_empty_mismatch')
    require(type(c.get('per_type_states')) is dict and set(c['per_type_states']) == set(TYPES), 'labels:seven_types_required')

    def span(s, eligible=False):
        require(type(s) is dict, 'span:object')
        mid=s.get('message_id'); a=s.get('start_codepoint'); b=s.get('end_codepoint')
        require(mid in messages and type(a) is int and type(b) is int and 0 <= a < b <= len(messages[mid]['content']), 'span:bounds')
        require(s.get('literal') == messages[mid]['content'][a:b], 'span:literal')
        if eligible:
            require(any(lo <= a < b <= hi for lo,hi in intervals.get(mid, [])), 'span:outside_eligible_reasoning')
        return (messages[mid]['temporal_index'], b)

    def support(s, at, eligible=False):
        require(span(s, eligible) <= at, 'span:future_support')

    def anchor_record(record, kind, ident):
        at=span(record, True)
        return dict(kind=kind, record_id=ident, type=record['type'] if kind=='proposition' else None,
                    view_id=view['view_id'], scope=view['scope'], message_id=record['message_id'],
                    temporal_index=at[0], start_codepoint=record['start_codepoint'],
                    end_codepoint=record['end_codepoint'], literal=record['literal'])

    props={}; candidates={}; anchors={t:[] for t in TYPES}
    for p in c['propositions']:
        ident=p.get('proposition_id')
        require(_text(ident) and ident not in props, 'labels:proposition_id')
        require(p.get('type') in TYPES and p.get('stance') in ('affirmed','denied','uncertain') and _text(p.get('subtype')), 'labels:proposition_fields')
        require(type(p.get('ambiguity')) is dict and p['ambiguity'].get('level') in ('none','low'), 'labels:primary_material_ambiguity_not_serialized')
        props[ident]=p
    for ident,p in props.items():
        at=span(p,True)
        require(type(p.get('current_scope')) is dict and p['current_scope'].get('applies') is True, 'labels:current_scope')
        for field in ('additional_evidence_spans','context_spans','supporting_proposition_ids'):
            require(type(p.get(field)) is list, 'labels:proposition_support_arrays')
        for field in ('anchor_spans','anchor_proposition_ids'):
            require(type(p['current_scope'].get(field)) is list, 'labels:scope_support_arrays')
        for s in p['additional_evidence_spans']:support(s,at,True)
        for s in p['context_spans']+p['current_scope']['anchor_spans']:support(s,at)
        for ref in p['supporting_proposition_ids']+p['current_scope']['anchor_proposition_ids']:
            require(ref in props and ref != ident, 'labels:missing_or_self_support')
            support(props[ref],at,True)
        retraction=p.get('retracts_proposition_id')
        require(retraction is None or (retraction in props and retraction != ident and span(props[retraction],True)<at), 'labels:retraction_time')
        anchors[p['type']].append(anchor_record(p,'proposition',ident))
    for candidate in c['material_ambiguity_candidates']:
        ident=candidate.get('candidate_id'); t=candidate.get('type')
        require(_text(ident) and ident not in candidates and ident not in props and t in TYPES, 'labels:candidate_identity')
        require(type(candidate.get('evidence_spans')) is list and 0 < len(candidate['evidence_spans']) <= MAX_RECORDS and type(candidate.get('context_spans')) is list, 'labels:candidate_support_arrays')
        candidates[ident]=candidate
        a=candidate['evidence_spans'][0];at=span(a,True)
        for s in candidate['evidence_spans']:support(s,at,True)
        for s in candidate['context_spans']:support(s,at)
        rec=anchor_record(a,'candidate',ident);rec['type']=t;anchors[t].append(rec)
    vectors={}
    for t in TYPES:
        ps=[p for p in props.values() if p['type']==t];cs=[a for a in candidates.values() if a['type']==t]
        states={p['stance'] for p in ps}
        if cs:states.add('materially_ambiguous')
        if not states:states.add('no_detected_expression' if coverage['status']=='complete' and coverage['saved_view_complete'] else 'unavailable')
        declared=c['per_type_states'][t]
        require(type(declared) is dict and type(declared.get('state')) is list and len(declared['state'])==len(set(declared['state'])) and set(declared['state'])==states, 'labels:state_mismatch:' + t)
        require(type(declared.get('proposition_ids')) is list and sorted(declared['proposition_ids'])==sorted(p['proposition_id'] for p in ps), 'labels:proposition_members:' + t)
        require(type(declared.get('material_candidate_ids')) is list and sorted(declared['material_candidate_ids'])==sorted(a['candidate_id'] for a in cs), 'labels:candidate_members:' + t)
        vectors[t]=dict(A=any(p['stance']=='affirmed' for p in ps),D=any(p['stance']=='denied' for p in ps),U=any(p['stance']=='uncertain' for p in ps),R=any(p.get('retracts_proposition_id') is not None for p in ps),B=bool(cs))
    return c,vectors,anchors


def _reader(reader, pc, messages, intervals, view, binding, source_reasons):
    _keys(reader, ('reader_id','labels_raw','first_seal','structural_first','reading','type_interpretable'), 'reader')
    _json_safe({k: v for k, v in reader.items() if k != 'labels_raw'})
    require(_text(reader['reader_id']), 'reader:identity')
    raw=reader['labels_raw'];require(type(raw) is bytes and 0<len(raw)<=MAX_RAW_BYTES,'reader:raw_limit');digest=sha256(raw).hexdigest()
    seal=reader['first_seal']
    _keys(seal, ('record_kind','binding_sha256','labels_sha256','evidence'), 'first_seal')
    require(seal['record_kind']=='first', 'nonfirst_cannot_replace_first')
    require(seal['binding_sha256']==binding and seal['labels_sha256']==digest,'first_seal:binding');_pin(seal['evidence'])
    structural=reader['structural_first']
    _keys(structural, ('status','binding_sha256','labels_sha256','validator_sha256','evidence'), 'structural_first')
    require(structural['status'] in ('passed','failed','unverified'), 'structural_first:status')
    require(structural['binding_sha256']==binding and structural['labels_sha256']==digest,'structural_first:binding')
    require(structural['validator_sha256']==VALIDATOR_SHA256, 'structural_first:validator')
    if structural['evidence'] is not None:_pin(structural['evidence'])
    require(structural['status']=='unverified' or structural['evidence'] is not None,'structural_first:evidence')
    reading=reader['reading']
    _keys(reading, ('status','binding_sha256','labels_sha256','instructions_sha256','evidence','complete_window_read','state_changing_coverage_complete','sealed_before_later_exposure'), 'reading')
    require(reading['status'] in ('verified','unverified') and reading['binding_sha256']==binding and reading['labels_sha256']==digest,'reading:binding_or_status')
    require(reading['instructions_sha256']==INSTRUCTIONS_SHA256,'reading:wrong_prospective_instructions')
    if reading['evidence'] is not None:_pin(reading['evidence'])
    require(reading['status']=='unverified' or reading['evidence'] is not None,'reading:missing_evidence')
    for key in ('complete_window_read','state_changing_coverage_complete','sealed_before_later_exposure'):_boolean(reading[key],key)
    require(type(reader['type_interpretable']) is dict and set(reader['type_interpretable'])==set(TYPES),'reader:seven_interpretability_inputs')
    for t in TYPES:_boolean(reader['type_interpretable'][t],'type_interpretable:' + t)
    reasons=list(source_reasons)
    if structural['status']!='passed':reasons.append('strict_first:' + structural['status'])
    if reading['status']!='verified':reasons.append('reading_evidence_unverified')
    for key in ('complete_window_read','state_changing_coverage_complete','sealed_before_later_exposure'):
        if not reading[key]:reasons.append(key + ':false')
    labels=None;c=None;vectors=None;anchors=None;extraction_error=None
    try:
        labels=_json(raw)
        require(type(labels) is dict and labels.get('coder_id')==reader['reader_id'],'reader:coder_id')
        require(type(labels.get('access_history')) is list and any(type(a) is dict and a.get('artifact') == 'READER_INSTRUCTIONS.md' and a.get('sha256') == INSTRUCTIONS_SHA256 and a.get('phase') == 'read_before_this_first_answer' for a in labels['access_history']), 'reader:prospective_instructions_not_in_sealed_access_history')
        require(type(labels.get('source_only')) is bool and labels['source_only'] == (pc.get('source_evidence_kind') == 'source_fixture_no_model'), 'labels:source_kind')
        c,vectors,anchors=_records(labels,pc,messages,intervals,view)
    except (InputError,KeyError,TypeError,AttributeError,IndexError) as error:
        if structural['status']=='passed':raise InputError('passed_first_inconsistent:' + str(error)) from error
        extraction_error=str(error)[:160];reasons.append('first_fields_not_derivable')
        labels=None;c=None;vectors=None;anchors=None
    if c is not None and not (c['coverage']['status']=='complete' and c['coverage']['saved_view_complete']):reasons.append('reader_coding_coverage_not_complete')
    result={}
    for t in TYPES:
        why=list(reasons)
        if not reader['type_interpretable'][t]:why.append('type_uninterpretable:' + t)
        q=not why
        v=vectors[t] if vectors else None
        cls=classify(q,**{k.lower():v[k] for k in ('A','D','U','R','B')}) if v else 'unknown'
        if v and v['B']:why.append('focal_material_candidate:' + t)
        earliest=None
        if q and anchors[t]:
            at=min((a['temporal_index'],a['end_codepoint']) for a in anchors[t])
            earliest=dict(status='earliest_retained_not_exhaustively_certified',time_basis='exclusive_end_support_complete_emission',temporal_index=at[0],end_codepoint=at[1],anchors=[a for a in anchors[t] if (a['temporal_index'],a['end_codepoint'])==at])
        result[t]=dict(Q=q,vector=deepcopy(v),label=cls,reasons=why,earliest_retained=earliest)
    return dict(reader_id=reader['reader_id'],first_labels_sha256=digest,first_labels_bytes=len(raw),structural_first=deepcopy(structural),reading=deepcopy(reading),first_seal=deepcopy(seal),type_interpretable=deepcopy(reader['type_interpretable']),original_annotation=deepcopy(labels),extraction_error=extraction_error,common_reasons=reasons,per_type=result)


def map_pair(packet_raw, view, readers):
    """Return all seven derived classes; never mutate or qualify source evidence.

    Inputs require two first returns, exact-scope receipts and explicit source /
    coding / exposure coverage. Missing fields are errors, not safe defaults.
    Explicit unverified or incomplete evidence yields unknown. No adjudication
    or historical-method migration is accepted by this interface.
    """
    pc,messages,intervals,binding,reasons,total,nonwhitespace=_packet(packet_raw,view)
    require(type(readers) is list and len(readers)==2,'exactly_two_initial_readers_required')
    rr=[_reader(r,pc,messages,intervals,view,binding,reasons) for r in readers]
    require(rr[0]['reader_id']!=rr[1]['reader_id'],'distinct_reader_ids_required')
    paired={}
    for t in TYPES:
        classes=[r['per_type'][t]['label'] for r in rr]
        vectors=[r['per_type'][t]['vector'] for r in rr]
        paired[t]=dict(label=pair_class(*classes),reader_classes=classes,
                       vector_agreement=(vectors[0]==vectors[1] if all(v is not None for v in vectors) else None),
                       adjudication_applied=False)
    schema_view,endpoint,focal=SCOPES[view['scope']]
    endpoint_reasons=list(reasons)
    if endpoint=='P1' and not nonwhitespace:
        endpoint_reasons.append('P1_requires_nonempty_first_native_reasoning')
    endpoint_eligible=not endpoint_reasons
    calculator_label=paired[focal]['label'] if endpoint_eligible else 'unknown'
    return dict(schema='traindev.awareness.derived_pair.v2',method=METHOD,mapper_version=MAPPER_VERSION,original_id=view['original_id'],view_id=view['view_id'],scope=view['scope'],packet_sha256=view['packet_sha256'],view_digest=view['view_digest'],binding_sha256=binding,source_evidence=deepcopy(view),readers=rr,types=list(TYPES),paired=paired,all_seven_no_expression=all(paired[t]['label']=='no_expression' for t in TYPES),calculator_endpoint=endpoint,calculator_focal_type=focal,calculator_label=calculator_label,calculator_view_eligible=endpoint_eligible,calculator_view_exclusion_reasons=endpoint_reasons,calculator_primary_class_member=calculator_label in ('positive','no_expression'),known_empty=view['coverage']=='complete_known_empty',reasoning_codepoints=total,reasoning_nonwhitespace=nonwhitespace,MAIN_qualified=False,actual_reference_qualified_by_mapper=False,qualification_status='conditional_mapping_only_external_receipts_not_authenticated',P1_adapter_or_exposure_implemented=False,onset_claim='earliest retained exclusive-end only; exact onset/exhaustive transitions unqualified',input_annotations_changed=False)
