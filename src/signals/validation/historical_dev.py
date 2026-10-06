"""Source-bound saved-public-response DEV description; no main-frame runner.

The caller supplies an externally authenticated request digest and actual saved
artifacts. This module cannot certify source adoption, human reference validity,
loaded model weights, native replay or authority to invoke historical analysis.
"""
from copy import deepcopy
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import ModuleType

SCOPE = 'historical_exposed_DEV_no_main_frame'
CONVENTION = 'sigres2-historical-dev-derived-bindings-v1'
SOURCE_PINS = {
    'adapter': (47301, 'fd815de587f55703bf27cfc27e7a230d6e946549890704656f4415acf3038361'),
    'primitives': (7354, 'af906e82ec9d90b9a8fef8eaf169855f848eaae254e5e4bf0a5aaa4baedece45'),
}
MAX_BYTES = 32 * 1024**2


class SourceError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise SourceError(reason)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def recorded_content_digest(value):
    """Exact saved runner digest convention; distinct from file/config pins."""
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode())


def descriptor(body):
    return {'bytes': len(body), 'sha256': digest(body)}


def derived_id(kind, value):
    """Authored foreign key, explicitly not a native recorded identity."""
    return 'sigres2-dev-v1-' + kind + '-' + digest(canonical(value))


def load_sources(adapter_source, primitives_source):
    loaded = []
    for key, path in [('adapter', adapter_source), ('primitives', primitives_source)]:
        path = Path(path)
        require(not path.is_symlink(), 'source module symlink')
        size, sha = SOURCE_PINS[key]
        with path.open('rb') as stream:
            body = stream.read(size + 1)
        require(len(body) == size and digest(body) == sha, 'authenticated source module changed')
        module = ModuleType('_sigres2_dev_' + key)
        module.__file__ = str(path)
        # Full authenticated source bytes, never timestamp-valid local pyc.
        # Neither module invokes the V3 analysis at import.
        exec(compile(body, str(path), 'exec'), module.__dict__)
        loaded.append(module)
    return tuple(loaded)


class Documents:
    def __init__(self, text_by_digest, adapter):
        require(type(text_by_digest) is dict and len(text_by_digest) <= 20000,
                'bounded document registry')
        require(all(type(k) is str and type(v) is str and digest(v.encode()) == k
                    for k, v in text_by_digest.items()), 'document registry byte identity')
        require(sum(len(v.encode()) for v in text_by_digest.values()) <= MAX_BYTES, 'document byte ceiling')
        self.text = dict(text_by_digest)
        self.adapter = adapter

    def raw(self, pin):
        require(type(pin) is dict and type(pin.get('bytes')) is int
                and 0 <= pin['bytes'] <= 8 * 1024**2 and type(pin.get('sha256')) is str,
                'source artifact descriptor')
        body = self.text[pin['sha256']].encode('utf-8')
        require(len(body) == pin['bytes'] and digest(body) == pin['sha256'], 'source artifact bytes changed')
        return body

    def json(self, pin):
        return self.adapter.strict_json(self.raw(pin))


def _key(value):
    return canonical(value).decode('utf-8')


def _indexed(rows, field):
    require(type(rows) is list, 'source row list required')
    out = {}
    for row in rows:
        require(type(row) is dict and type(row.get(field)) is str and row[field]
                and row[field] not in out, 'duplicate or invalid source ID: ' + field)
        out[row[field]] = row
    return out


def _saved_runner_message(response):
    """Pinned runner.py:185-189 projection, not fresh response qualification.

    Source 3b3fb6db5495d523518a60f7eaa636baa409ba1e95c5329a06985c85532a0e4c;
    retain optional nulls and reasoning aliases, default only absent content.
    """
    require(type(response) is dict and type(response['choices']) is list
            and len(response['choices']) == 1, 'one saved completion choice')
    source = response['choices'][0]['message']
    require(type(source) is dict and source.get('role') == 'assistant', 'saved assistant role')
    allowed = ('role', 'content', 'tool_calls', 'reasoning_content', 'reasoning', 'refusal')
    message = {key: deepcopy(source[key]) for key in allowed if key in source}
    message.setdefault('content', None)
    return message


def _saved_runner_request_prefix(native):
    """Exact runner.py:229-235 replay projection; raw/native stay separate."""
    allowed = {'role', 'content', 'tool_calls', 'tool_call_id', 'reasoning_content', 'reasoning'}
    return [{key: deepcopy(value) for key, value in message.items() if key in allowed}
            for message in native]


def _global_sources(history, request, docs, adapter):
    scope = docs.json(request['owner_scope'])
    require(scope['scope'] == SCOPE and scope['selection']['documentary_views'] == 32
            and scope['selection']['actual_original_parents'] == 2
            and scope['selection']['primary_pairs'] == 14
            and scope['selection']['secondary_audience_control_pairs'] == 2
            and scope['selection']['denominator_per_primary_type'] == 2, 'fixed limited scope')
    table = docs.json(request['transport_table'])
    audit = docs.json(request['technical_audit'])
    require(table['scope'] == audit['scope'] == SCOPE, 'source scope drift')
    # The reviewed audit locates raw evidence; truth-valued audit fields are
    # not substituted for the joins below.
    plan = docs.json(request['saved_plan'])
    ledger = docs.json(request['saved_ledger'])
    runtime = docs.json(request['saved_runtime'])
    plan_content = recorded_content_digest(plan)
    require(plan_content == ledger['plan_sha256'] == runtime['plan_sha256']
            == audit['plan_content_sha256'], 'saved plan/ledger/runtime content join')
    selection = docs.json(request['all_assigned_selection'])
    require(len(plan['rows']) == len(ledger['rows']) == 22 and len(ledger['calls']) == 32,
            'all execution assignments and calls retained')
    require(history['analysis_scope'] == SCOPE and len(history['records']) == 32
            and len(history['raw_provenance']) == len(history['assignment_ledger']) == 32,
            'complete source history')
    plan_rows = _indexed(plan['rows'], 'id')
    require({x['id'] for x in plan['rows']} == {x['assignment']['id'] for x in ledger['rows']}
            and all(plan_rows[x['assignment']['id']] == x['assignment'] for x in ledger['rows']),
            'execution audit assignments')
    frozen_audit = table['all_assigned_execution_audit']
    require(frozen_audit['ledger_rows'] == ledger['rows']
            and frozen_audit['selection_rows'] == selection
            and frozen_audit['call_count'] == len(ledger['calls'])
            and frozen_audit['plan_content_sha256'] == plan_content
            and frozen_audit['plan_file_sha256'] == request['saved_plan']['sha256']
            and all(frozen_audit['ledger_pin'][k] == request['saved_ledger'][k] for k in ('bytes', 'sha256')),
            'full frozen execution audit')
    require({x['assignment_id'] for x in selection} == set(plan_rows)
            and len(selection) == 22, 'all assigned selection IDs')
    source_keys = [_key(row['source_key']) for row in history['records']]
    require(len(set(source_keys)) == 32 and source_keys == [_key(row['source_key']) for row in table['rows']]
            == [_key(row['source_key']) for row in audit['view_links']], 'frozen documentary row order')
    require(all(_key(a['source_key']) == key == _key(b['source_key'])
                for key, a, b in zip(source_keys, history['raw_provenance'], history['assignment_ledger'])),
            'sidecar provenance order')
    raw_rows = [x['raw_worklist_record'] for x in history['raw_provenance']]
    require(len({r['parent_id'] for r in raw_rows}) == 2 and len({r['item_id'] for r in raw_rows}) == 18
            and sum(len(r['raw_components_in_type_frame']) for r in raw_rows) == 52, 'fixed physical/component population')
    require(len(table['primary_pairs']) == 14 and len(table['audience_control_pairs']) == 2,
            'frozen primary/control pairs')
    require(table['all_assigned_execution_audit']['selection_pin']['sha256'] == request['all_assigned_selection']['sha256'],
            'source selection identity')
    actor = plan['actor']
    require(set(actor) == {'model', 'model_revision', 'generation'}
            and actor == audit['recorded_actor_components'], 'recorded shared actor configuration')
    config = {'schema': CONVENTION, 'model': actor['model'], 'recorded_model_revision': actor['model_revision'],
              'shared_generation': deepcopy(actor['generation']),
              'per_call_fields_excluded': ['seed', 'max_tokens', 'tool_choice', 'tools'],
              'loaded_weights_attested': False}
    config_raw = canonical(config)
    config_pin = descriptor(config_raw)
    config_id = derived_id('actor', config_pin)
    return {'scope': scope, 'table': table, 'audit': audit, 'plan': plan, 'ledger': ledger,
            'runtime': runtime, 'selection': selection, 'config': config, 'config_pin': config_pin,
            'actor_id': config_id, 'plan_content_sha256': plan_content}


def _bind_view(sidecar, auxiliary, view_index, shared, docs, adapter):
    key = sidecar['source_key']
    raw = auxiliary['raw_worklist_record']
    audit = shared['audit']
    located = audit['physical_reader_items'][key['item_id']]
    require(located['parent_id'] == raw['parent_id']
            and audit['view_links'][view_index]['source_key'] == key, 'reader/parent view binding')
    reader_pin = auxiliary['source_item_pin']
    original_pin = auxiliary['source_original_item_pin']
    require(all(reader_pin[k] == located['source_item_pin'][k] for k in ('bytes', 'sha256'))
            and all(original_pin[k] == located['original_source_item_pin'][k] for k in ('bytes', 'sha256')),
            'reader artifact pins')
    reader, original = docs.json(reader_pin), docs.json(original_pin)
    require(reader['item_id'] == key['item_id'] and reader['designated_channel'] == 'public_text', 'public reader identity')
    native, parent_native = reader['native_messages_at_cutoff'], original['native_messages_at_cutoff']
    cutoff, parent_cutoff = reader['cutoff_exclusive_message_index'], original['cutoff_exclusive_message_index']
    require(type(cutoff) is int and type(parent_cutoff) is int
            and cutoff == len(native) and parent_cutoff == len(parent_native)
            and native[:parent_cutoff] == parent_native, 'exact original/cutoff prefix')
    require(reader['eligible_message_indices'] == raw['source_item_identity']['eligible_message_indices']
            == located['eligible_native_message_indices']
            and cutoff == raw['source_item_identity']['cutoff_exclusive_message_index']
            and reader['availability'] == raw['source_item_identity']['availability'], 'eligible native coverage/availability')
    parent = audit['parents'][raw['parent_id']]
    trajectory_pin = parent['trajectory_pin']
    trajectory = docs.json(trajectory_pin)
    require(trajectory_pin['sha256'] == raw['source_item_identity']['source_trajectory']['sha256']
            and trajectory['initial_context'] + trajectory['messages'] == parent_native,
            'original trajectory file/history join')
    assignment = trajectory['assignment']
    require(assignment == parent['assignment_copied'] and assignment['assignment_id'] == raw['parent_id'],
            'recorded original assignment')
    plan_rows = {r['id']: r for r in shared['plan']['rows']}
    require(plan_rows[raw['parent_id']]['assignment'] == assignment, 'plan/original assignment')
    case = docs.json(parent['case_body_pin'])
    case_row = audit['cases'][assignment['case_id']]
    require(parent['case_body_pin']['sha256'] == case_row['pin']['sha256']
            and recorded_content_digest(case) == case_row['content_sha256'] == trajectory['case_sha256'], 'case file/content identity')
    actor = shared['plan']['actor']
    require({k: trajectory[k] for k in ('model', 'model_revision', 'generation')} == actor,
            'recorded original actor configuration')
    source_assignment = located['source_assignment_id']
    require(source_assignment in plan_rows, 'actual view assignment in full audit')
    request_records = []
    for call in audit['selected_calls']:
        if call['assignment_id'] != source_assignment:
            continue
        ledger_call = adapter.json_pointer(shared['ledger'], call['ledger_pointer'])
        req, resp = docs.json(call['request_pin']), docs.json(call['response_pin'])
        require(ledger_call['assignment_id'] == source_assignment
                and ledger_call['request_sha256'] == call['request_pin']['sha256']
                and ledger_call['response_sha256'] == call['response_pin']['sha256'], 'original request/response ledger pins')
        require(req['model'] == resp['model'] == actor['model']
                and all(req[k] == value for k, value in actor['generation'].items()), 'saved request shared configuration')
        request_records.append({'request': deepcopy(call['request_pin']), 'response': deepcopy(call['response_pin']),
                                'settings': {k: deepcopy(v) for k, v in req.items() if k != 'messages'},
                                'recorded_status': ledger_call['status'], 'request_object': req, 'response_object': resp})
    require(len(request_records) == (4 if key['view'] == 'E_public' else 1), 'all selected native calls')
    if key['view'] == 'E_public':
        require(source_assignment == raw['parent_id'] and native == parent_native, 'original view history')
        require([len(r['request_object']['messages']) for r in request_records] == [2, 4, 6, 8],
                'original serial request positions')
        for record in request_records:
            pos = len(record['request_object']['messages'])
            require(record['request_object']['messages'] == _saved_runner_request_prefix(native[:pos])
                    and _saved_runner_message(record['response_object']) == native[pos],
                    'actual original runner-projected request prefix and response')
    else:
        prepared = docs.json(located['saved_ASK_joins']['prepared_pin'])
        request, response = request_records[0]['request_object'], request_records[0]['response_object']
        require(prepared['assignment_id'] == source_assignment and prepared['original_assignment_id'] == raw['parent_id']
                and prepared['model_revision'] == actor['model_revision'] and prepared['request'] == request,
                'saved ASK preparation binding')
        require(request['messages'][:parent_cutoff] == parent_native
                and len(request['messages']) == parent_cutoff + 1
                and request['messages'] + [response['choices'][0]['message']] == native,
                'actual ASK original-only prefix and reply')
        require(digest(request['messages'][-1]['content'].encode()) == prepared['question_sha256'], 'actual ASK question')
        planned = plan_rows[source_assignment]
        require(planned['parent_assignment_id'] == raw['parent_id']
                and planned['question']['text'] == request['messages'][-1]['content']
                and planned['question']['sha256'] == prepared['question_sha256']
                and planned['seed'] == request['seed']
                and planned['per_response_cap'] == request['max_tokens'], 'ASK plan question/settings')
    original_id = derived_id('trajectory', {'source_assignment_id': raw['parent_id'], 'file': descriptor(docs.raw(trajectory_pin))})
    view_id = original_id if key['view'] == 'E_public' else derived_id('trajectory', {'source_assignment_id': source_assignment, 'reader': descriptor(docs.raw(reader_pin))})
    original_cutoff_id = derived_id('cutoff', {'trajectory_id': original_id, 'reader': descriptor(docs.raw(original_pin)), 'pointer': '/native_messages_at_cutoff', 'event_count': parent_cutoff})
    cutoff_id = original_cutoff_id if key['view'] == 'E_public' else derived_id('cutoff', {'trajectory_id': view_id, 'reader': descriptor(docs.raw(reader_pin)), 'pointer': '/native_messages_at_cutoff', 'event_count': cutoff})
    texts = {}
    for row in reader['target_texts']:
        index = row['message_index']
        require(type(index) is int and index not in texts and type(row['text']) is str, 'unique literal target text')
        texts[index] = row['text']
    channels, maps = [], []
    eligible = reader['eligible_message_indices']
    require(len(set(eligible)) == len(eligible) and set(texts) <= set(eligible), 'public position coverage')
    for index in eligible:
        require(type(index) is int and 0 <= index < cutoff and native[index]['role'] == 'assistant', 'public native position')
        msg = native[index]
        present = 'content' in msg
        content = msg.get('content')
        require(content is None or type(content) is str, 'supported explicit public content type')
        require((type(content) is str and texts.get(index) == content) or
                (content is None and index not in texts), 'native/text null distinction')
        channel_id = derived_id('channel', {'reader_sha256': reader_pin['sha256'], 'native_index': index})
        channel = {'channel_item_id': channel_id, 'trajectory_id': view_id, 'event_index': index,
                   'native_message_index': index, 'channel': 'public_assistant', 'origin': 'actor_generated',
                   'content_present': present, 'content_type': 'absent' if not present else 'null' if content is None else 'string',
                   'text': content, 'text_sha256': None if content is None else digest(content.encode()),
                   'source_reader_pin': deepcopy(reader_pin), 'source_pointer': '/native_messages_at_cutoff/%d/content' % index,
                   'empty_string': type(content) is str and content == ''}
        channels.append(channel)
        maps.append({'source_message_index': index, 'corpus_event_index': index, 'channel_item_id': channel_id,
                     'source_text_sha256': channel['text_sha256'], 'content_type': channel['content_type']})
    by_index = {r['native_message_index']: r for r in channels}
    spans = []
    for literal in auxiliary['literal_spans']:
        ev = literal['evidence']; text = by_index[ev['message_index']]['text']
        require(type(text) is str, 'literal cannot use absent public content')
        a, b, u, v = (ev[k] for k in ('start_codepoint', 'end_codepoint', 'start_utf8_byte', 'end_utf8_byte'))
        require(all(type(x) is int for x in (a, b, u, v)) and 0 <= a < b <= len(text)
                and text[a:b] == ev['quote'] and text.encode()[u:v] == ev['quote'].encode()
                and len(text[:a].encode()) == u and len(text[:b].encode()) == v,
                'exact literal Unicode/UTF8 source span')
        spans.append({'span_id': derived_id('span', {'source_key': key, 'proposition_id': literal['proposition_id']}),
                      'source_proposition_id': literal['proposition_id'], 'channel_item_id': by_index[ev['message_index']]['channel_item_id'],
                      'start_unicode': a, 'end_unicode': b, 'start_utf8': u, 'end_utf8': v,
                      'literal_text': ev['quote'], 'literal_sha256': digest(ev['quote'].encode())})
    require({r['source_proposition_id'] for r in spans} == {r['proposition_id'] for r in sidecar['raw_propositions']},
            'all source components have literal spans')
    return {'convention': CONVENTION, 'IDs_are_authored_not_native': True, 'actor_model_id': shared['actor_id'],
            'config_sha256': shared['config_pin']['sha256'], 'recorded_case_id': assignment['case_id'],
            'source_assignment_id': source_assignment, 'original_source_assignment_id': raw['parent_id'],
            'original_trajectory_id': original_id, 'view_trajectory_id': view_id,
            'original_cutoff_id': original_cutoff_id, 'view_cutoff_id': cutoff_id, 'event_count': cutoff,
            'native_index_to_event_convention': 'Every native message has one event at the same zero-based index; none is removed.',
            'native_prefix_artifact': deepcopy(reader_pin), 'native_prefix_messages_pointer': '/native_messages_at_cutoff',
            'original_native_prefix_artifact': deepcopy(original_pin), 'original_native_prefix_messages_pointer': '/native_messages_at_cutoff',
            'native_message_to_corpus_event_map': maps, 'channels': channels, 'evidence_spans': spans,
            'source_identity_verified': True, 'literal_source_verified': True, 'saved_public_history_verified': True,
            'recorded_availability': reader['availability'], 'recorded_generation_finish_reason': reader.get('generation_finish_reason'),
            'content_coverage': {'eligible_positions': list(eligible), 'text_positions': sorted(texts),
                                 'nontext_positions': [c['native_message_index'] for c in channels if c['content_type'] != 'string'],
                                 'does_not_infer_complete_view': True},
            'call_settings': [{k: v for k, v in r.items() if k not in {'request_object', 'response_object'}} for r in request_records],
            'stronger_replay_qualified': None, 'loaded_weights_attested': False}


def _owner_provenance(supplied, consumer_pin, request, docs, adapter, literal_ids):
    # These are the explicit adopted-input roster and consumer record pins in
    # the externally frozen request, not arbitrary evidence booleans.
    adopted = {p['sha256'] for p in request['adopted_owner_inputs']}
    require(supplied['owner_input_refs'], 'actual owner input provenance required')
    for ref in supplied['owner_input_refs']:
        require(ref['artifact_sha256'] in adopted, 'owner decision not in frozen adopted-input roster')
    adapter._refs(supplied['owner_input_refs'], docs.text, literal_ids, required=True)
    for pin in request['adopted_owner_inputs']:
        docs.raw(pin)
    adoptions = [docs.json(pin) for pin in request['owner_adoption_records']]
    covered = set()
    adoption_times = {}
    envelope_adoptions = []
    envelope_key = (consumer_pin['bytes'], consumer_pin['sha256'])
    for adoption_pin, adoption in zip(request['owner_adoption_records'], adoptions):
        require(adoption['scientific_owner'] in ('sigres', 'sigres2')
                and adoption['decision'] in {
                    'adopt_explicit_authored_inputs_for_bounded_historical_DEV_integration_only',
                    'adopt_exact_authored_inputs_for_bounded_historical_DEV_integration_only'}
                and all(adoption['scope'][k] == request['owner_scope'][k] for k in ('bytes', 'sha256'))
                and adapter._utc(adoption['recorded_at_utc']) <= adapter._utc(request['analysis_cutoff']),
                'owner input adoption scope/cutoff')
        candidates = adoption.get('candidates', [adoption.get('candidate')])
        keys = {(p['bytes'], p['sha256']) for p in candidates if type(p) is dict}
        covered.update(keys)
        stamp = adapter._utc(adoption['recorded_at_utc'])
        for key in keys:
            adoption_times.setdefault(key, []).append(stamp)
        if envelope_key in keys:
            envelope_adoptions.append({'record_pin': deepcopy(adoption_pin),
                                       'recorded_at_utc': adoption['recorded_at_utc']})
    require({(p['bytes'], p['sha256']) for p in request['adopted_owner_inputs']} <= covered,
            'missing exact owner input adoption record')
    # The complete operative translation is itself an adopted input. Its pin
    # commits every semantic value and the hashes of reference, actor, scope,
    # admission and positive-scope records. Merely citing an earlier adopted
    # document cannot authorize replacing its nulls or alternative decisions.
    require(docs.json(consumer_pin) == supplied
            and envelope_key in covered,
            'exact translated consumer envelope lacks owner adoption')
    referenced = {ref['artifact_sha256'] for ref in supplied['owner_input_refs']}
    latest_input_adoption = max(max(adoption_times[(pin['bytes'], pin['sha256'])])
                                for pin in request['adopted_owner_inputs'] if pin['sha256'] in referenced)
    require(max(adoption_times[envelope_key]) >= latest_input_adoption,
            'translated envelope adoption predates referenced owner input adoption')
    return {'owner_input_artifacts': [
                {'pin': deepcopy(pin), 'original_utf8_text': docs.raw(pin).decode('utf-8')}
                for pin in request['adopted_owner_inputs'] if pin['sha256'] in referenced],
            'translated_envelope_adoptions': envelope_adoptions}


def _owner_ref_coverage(refs, supplied, request):
    """Every operative owner-source ref must be in the declared dependency set.

    This checks the refs actually used by the finite validators below, not a
    recursive traversal of arbitrary source documents or historical prose.
    Source shape/pointer checks remain with the accepted validators.
    """
    owner = {p['sha256'] for p in request['adopted_owner_inputs']}
    declared = {r['artifact_sha256'] for r in supplied['owner_input_refs']}
    if type(refs) is list:
        for ref in refs:
            if type(ref) is dict and ref.get('artifact_sha256') in owner:
                require(ref['artifact_sha256'] in declared,
                        'operative owner input absent from declared owner_input_refs: ' + ref['artifact_sha256'])


def _project(sidecar, auxiliary, binding, supplied, consumer_pin, request, docs, adapter, primitives, progress):
    def stage(name):
        progress['active_stage'] = name

    def done(status='verified_supplied_bytes'):
        progress['stages'].append({'stage': progress['active_stage'], 'status': status})

    stage('adopted_input_provenance')
    require(supplied['source_key'] == sidecar['source_key'], 'consumer source key')
    literals = {p['proposition_id'] for p in sidecar['raw_propositions']}
    progress.update(_owner_provenance(supplied, consumer_pin, request, docs, adapter, literals))
    progress['adopted_consumer_envelope_pin'] = deepcopy(consumer_pin)
    done()
    stage('reference_identity_and_mechanical_projection')
    reference = docs.json(supplied['reference_pin'])
    progress['reference_pin'] = deepcopy(supplied['reference_pin'])
    progress['supplied_reference'] = deepcopy(reference)
    require(reference['source_key'] == sidecar['source_key']
            and reference['actor_model_id'] == binding['actor_model_id']
            and reference['config_sha256'] == binding['config_sha256']
            and reference['view_cutoff_id'] == binding['view_cutoff_id']
            and reference['original_cutoff_id'] == binding['original_cutoff_id'], 'reference target identity')
    require(reference['source_record_sha256'] == sidecar['source_record']['record_sha256'], 'reference raw annotation line')
    require(reference['reference_provenance'] == auxiliary['reference_provenance']
            and reference['reader_id'] == auxiliary['source_annotation']['reader_id']
            and reference['human_reader'] is auxiliary['source_annotation']['human_reader'] is False,
            'unchanged historical reader provenance')
    require(reference['status'] in {'resolved', 'coding_unresolved', 'unavailable'}, 'explicit reference status')
    require(type(reference['complete_view_review']) is bool, 'explicit complete-view decision')
    props = _indexed(reference['propositions'], 'proposition_id')
    spans = {p['span_id']: p for p in binding['evidence_spans']}
    matches, ambiguous, unmapped_target = [], [], []
    for pid, prop in props.items():
        require(pid in literals and prop['span_id'] in spans and spans[prop['span_id']]['source_proposition_id'] == pid,
                'reference literal identity')
        # Null is the existing mapping contract's unresolved value, not an
        # invented actor ID or categorical replacement. The strict accepted
        # Q predicate is called only on completely supplied target fields.
        missing = [field for field in adapter.MAPPING_FIELDS if prop[field] is None]
        require(all(prop[field] is None or type(prop[field]) is str and prop[field]
                    and (allowed is None or prop[field] in allowed)
                    for field, allowed in adapter.MAPPING_FIELDS.items()), 'explicit mapped value/null only')
        if missing:
            unmapped_target.extend(pid + ':' + field for field in missing)
            ambiguous.append(pid)
            continue
        if primitives.qualifies_supplied_proposition(prop, binding['actor_model_id']):
            matches.append(pid)
        if prop['interpretation_kind'] == 'actor_assertion' and prop['polarity'] == 'affirmative' and prop['certainty'] == 'asserted' and 'unknown' in (prop['attributed_actor'], prop['referent'], prop['target_time']):
            ambiguous.append(pid)
    state = reference['expression_state']
    require(state in {'asserted', 'denied', 'uncertain', 'mixed', 'no_relevant_statement', 'unavailable', 'coding_unresolved'},
            'explicit reference summary state')
    # A recorded protocol null is not text and cannot support a literal. It
    # does not independently negate the reader's complete-view decision.
    # Recorded partial/unavailable views never become complete by this path.
    readable = binding['recorded_availability'] == 'readable'
    candidate, reason = None, 'incomplete_or_unresolved_reference'
    if readable and reference['complete_view_review'] and reference['status'] == 'resolved' and state not in {'unavailable', 'coding_unresolved'}:
        if state == 'mixed' and len(props) < 2:
            reason = 'mixed_without_explicit_propositions'
        elif state in {'asserted', 'denied', 'uncertain'} and not props:
            reason = 'stance_without_explicit_proposition'
        elif ambiguous and not matches:
            reason = 'unresolved_assertion_attribution_or_scope'
        else:
            candidate, reason = int(bool(matches)), 'explicit_complete_limited_reference_projection'
    projection = {'qualifying_assertion_ids': matches, 'value': candidate,
                  'reason': reason, 'ambiguous_assertion_ids': ambiguous,
                  'unmapped_target_fields': unmapped_target}
    progress['mechanical_projection'] = deepcopy(projection)
    done('provisional_predicate_output_not_reconciled_endpoint')
    stage('explicit_field_mapping')
    mapping_input = {'mapping_decisions': supplied['mapping_decisions'], 'target_binding': {
        'native_message_to_corpus_event_map': binding['native_message_to_corpus_event_map']}}
    for decision in supplied['mapping_decisions']:
        if type(decision) is dict:
            for field in ('supporting_source_refs', 'counterevidence_source_refs'):
                _owner_ref_coverage(decision.get(field), supplied, request)
    unresolved, established = adapter._mapping_check(sidecar, auxiliary, mapping_input, projection, reference,
        docs.text, {'corpus': {'evidence_spans': binding['evidence_spans']}})
    progress.update(established_assertion_ids=established, unresolved_fields=unresolved,
                    raw_unresolved_alternatives=deepcopy(sidecar['source_statuses']['unresolved_alternatives']),
                    mapping_decisions=deepcopy(supplied['mapping_decisions']))
    done()
    # Literal equality does not fill the deliberately separate semantic actor
    # attribution and within-view same_actor decision.
    stage('formal_actor_attribution')
    actor_record = docs.json(supplied['actor_attribution_pin'])
    require(actor_record['source_key'] == sidecar['source_key']
            and actor_record['actor_model_id'] == binding['actor_model_id']
            and actor_record['config_sha256'] == binding['config_sha256']
            and actor_record['technical_binding_sha256'] == digest(canonical(binding)), 'formal attribution binding')
    require(actor_record['same_actor'] is None or type(actor_record['same_actor']) is bool,
            'explicit semantic same_actor decision')
    require(all(type(actor_record.get(k)) is str and actor_record[k] for k in ('author_id', 'version')), 'attribution provenance')
    _owner_ref_coverage(actor_record['supporting_source_refs'], supplied, request)
    adapter._refs(actor_record['supporting_source_refs'], docs.text, literals, required=True)
    require(all(ref['artifact_sha256'] in {p['sha256'] for p in request['adopted_owner_inputs']}
                for ref in actor_record['owner_input_refs']), 'attribution owner source roster')
    _owner_ref_coverage(actor_record['owner_input_refs'], supplied, request)
    adapter._refs(actor_record['owner_input_refs'], docs.text, literals, required=True)
    require(sorted(actor_record['attributed_proposition_ids']) == sorted(props), 'complete proposition attribution provenance')
    progress['actor_attribution'] = deepcopy(actor_record)
    done()
    stage('complete_view_and_summary_scope')
    complete = supplied['complete_view_review_decision']
    require(type(complete['result']) is bool and complete['result'] == reference['complete_view_review']
            and all(type(complete.get(k)) is str and complete[k] for k in ('author_id', 'decision_version')),
            'complete-view provenance')
    _owner_ref_coverage(complete['supporting_source_refs'], supplied, request)
    adapter._refs(complete['supporting_source_refs'], docs.text, literals, required=True)
    rule = supplied['summary_rule']
    require(rule['schema_sha256'] == sidecar['summary_rule']['schema_sha256']
            and rule['rubric_sha256'] == sidecar['summary_rule']['rubric_sha256']
            and type(rule['aggregation_rule_id']) is str and rule['aggregation_rule_id'], 'source-backed aggregation rule')
    scope = rule['declared_scope']
    scope_record = docs.json(scope['provenance_record_pin'])
    require(all(type(scope_record.get(k)) is str and scope_record[k] for k in ('author_id', 'version')),
            'summary-scope author/version required')
    _owner_ref_coverage(scope_record['supporting_source_refs'], supplied, request)
    adapter._refs(scope_record['supporting_source_refs'], docs.text, literals, required=True)
    adopted_inputs = {p['sha256'] for p in request['adopted_owner_inputs']}
    require(all(ref['artifact_sha256'] in adopted_inputs for ref in scope_record['supporting_source_refs']),
            'summary-scope references must bind adopted owner inputs')
    comparability = supplied['scope_comparability']
    require(scope_record['source_key'] == sidecar['source_key']
            and scope_record['aggregation_rule_id'] == rule['aggregation_rule_id']
            and scope_record['scope_comparability'] == comparability
            and scope_record['exclusive_operational_summary'] is scope['exclusive_operational_summary']
            and comparability['same_actor'] is actor_record['same_actor'], 'exact within-view summary/actor scope')
    progress.update(complete_view_decision=deepcopy(complete), summary_scope=deepcopy(scope_record))
    done()
    stage('limited_reference_admission')
    admission = docs.json(supplied['reference_admission']['provenance_record_pin'])
    require(admission['decision_id'] == supplied['reference_admission']['decision_id']
            and admission['permitted_analysis_scope'] == supplied['reference_admission']['permitted_analysis_scope'] == SCOPE
            and admission['source_key'] == sidecar['source_key']
            and admission['reference_sha256'] == supplied['reference_pin']['sha256'], 'exact limited reference admission')
    done()
    stage('finite_reconciliation_and_cutoff')
    require(supplied['analysis_cutoff'] == request['analysis_cutoff'], 'frozen prospective analysis cutoff')
    ledger = supplied['reconciliation_ledger']
    require(type(ledger) is list and len(ledger) <= 1, 'one finite reconciliation record required')
    if ledger:
        require(type(ledger[0]) is dict and type(ledger[0].get('changes')) is list
                and all(type(change) is dict for change in ledger[0]['changes']),
                'reconciliation changes must be a list of objects')
        for field in ('initial_decision_refs', 'supporting_source_refs', 'counterevidence_source_refs'):
            _owner_ref_coverage(ledger[0].get(field), supplied, request)
        _owner_ref_coverage([ledger[0].get('adjudicator_initial_ref')], supplied, request)
    state, pending = adapter._reconciliation(sidecar, supplied, reference, docs.text, request['frozen_DEV_selection_sha256'])
    progress.update(reconciliation_pending=pending, reconciled_summary_state=state)
    done('pending' if pending else 'finite_ledger_checked')
    stage('positive_scope_and_DEV_reconciliation')
    does_not_undermine = False
    if supplied['positive_scope_record_pin'] is not None:
        positive = docs.json(supplied['positive_scope_record_pin'])
        require(positive['source_key'] == sidecar['source_key']
                and positive['mechanical_output_sha256'] == digest(canonical(projection))
                and positive['established_assertion_ids'] == established
                and positive['unresolved_fields'] == unresolved
                and type(positive['unresolved_does_not_undermine_Q']) is bool,
                'positive-scope exact mechanical join')
        require(all(type(positive.get(k)) is str and positive[k] for k in ('author_id', 'version')), 'positive-scope provenance')
        _owner_ref_coverage(positive['supporting_source_refs'], supplied, request)
        adapter._refs(positive['supporting_source_refs'], docs.text, literals, required=True)
        does_not_undermine = positive['unresolved_does_not_undermine_Q']
    evidence = {'source_identity_verified': True, 'literal_source_verified': True,
                'saved_public_history_verified': True, 'complete_view_review': readable and complete['result'],
                'reference_resolved': reference['status'] == 'resolved' and not pending,
                'mapping_audit_complete': True}
    qa = primitives.reconcile_limited_dev(candidate=candidate, summary_state=state, comparability=comparability,
        exclusive_summary_rule=scope['exclusive_operational_summary'], evidence=evidence,
        established_Q=bool(established), unresolved_fields=unresolved, admission_scope=SCOPE,
        unresolved_does_not_undermine_Q=does_not_undermine, stronger_replay_qualified=None)
    progress['reconciled_DEV'] = qa
    done(qa['disposition'])
    return progress


def _pair_bounds(e, r):
    """Enumerate retained-record logical bounds for (1-E)*R, count units."""
    require(all(x is None or type(x) is int and x in (0, 1) for x in (e, r)), 'binary/null pair')
    possible = [(1-a)*b for a in ((0, 1) if e is None else (e,)) for b in ((0, 1) if r is None else (r,))]
    return [min(possible), max(possible)]


def _describe_pairs(rows, history, table, adapter):
    by_key = {_key(row['source_key']): row for row in rows}
    raw = [a['raw_worklist_record'] for a in history['raw_provenance']]
    groups = {}
    for row in raw:
        group = (row['parent_id'], row['source_type'])
        groups.setdefault(group, {})[row['proposed_view_name']] = adapter._key(row, history['source_manifest_sha256'])
    require(len(groups) == 16 and all(set(v) == {'E_public', 'R_ASK'} for v in groups.values()), 'all paired documentary views')
    primary, audience = [], []
    for (parent, typ), views in groups.items():
        e, r = [by_key[_key(views[v])]['reconciled_DEV_value'] for v in ('E_public', 'R_ASK')]
        row = {'parent_id': parent, 'source_type': typ, 'E_public': e, 'R_ASK': r,
               'logical_count_bounds': _pair_bounds(e, r), 'views': views,
               'parent_independence_qualified': False, 'main_assigned_weight': None}
        (audience if adapter.TYPE_MAP[typ] is None else primary).append(row)
    require(len(primary) == 14 and len(audience) == 2, 'primary/control separation')
    summaries = {}
    for typ in adapter.TYPE_MAP:
        if adapter.TYPE_MAP[typ] is None:
            continue
        selected = [r for r in primary if r['source_type'] == typ]
        require(len(selected) == 2 and len({r['parent_id'] for r in selected}) == 2, 'fixed two-parent denominator')
        cells = {str(e)+','+str(r): sum(x['E_public'] == e and x['R_ASK'] == r for x in selected)
                 for e in (0, 1, None) for r in (0, 1, None)}
        resolved = sum(x['E_public'] is not None and x['R_ASK'] is not None for x in selected)
        summaries[typ] = {'assigned_parent_count': 2, 'resolved_pair_count': resolved, 'missing_pair_count': 2-resolved,
                          'E_resolved_count': sum(x['E_public'] is not None for x in selected),
                          'R_resolved_count': sum(x['R_ASK'] is not None for x in selected), 'joint_0_1_null_counts': cells,
                          'logical_count_bounds': [sum(x['logical_count_bounds'][i] for x in selected) for i in (0, 1)],
                          'scope': SCOPE, 'confidence_interval': None, 'main_weight': None}
    return primary, audience, summaries


def _recover_execution_audit(request, docs):
    """Preserve independently readable audit pieces, never infer a passed join."""
    sources = {}
    for name in ('transport_table', 'saved_plan', 'saved_ledger', 'saved_runtime', 'all_assigned_selection'):
        record = {'requested_pin': deepcopy(request.get(name)), 'byte_identity_verified': False,
                  'shared_join_verified': False, 'selected_raw_records': None,
                  'authenticated_raw_body': None, 'component_status': {}, 'errors': []}
        sources[name] = record
        try:
            body = docs.json(request[name])
            record['byte_identity_verified'] = True
            record['authenticated_raw_body'] = deepcopy(body)
            if name == 'transport_table':
                selected = body['all_assigned_execution_audit']
            elif name in {'saved_plan', 'saved_ledger'}:
                require(type(body) is dict, 'audit source body must be an object')
                fields = {'rows': list, 'actor': dict} if name == 'saved_plan' else {
                    'rows': list, 'calls': list, 'plan_sha256': str}
                selected = {}
                for field, expected_type in fields.items():
                    present = field in body
                    valid = present and type(body[field]) is expected_type
                    record['component_status'][field] = {'present': present, 'shape_valid': valid}
                    if present:
                        selected[field] = deepcopy(body[field])
                    if not valid:
                        record['errors'].append({'component': field, 'reason': 'missing' if not present else 'malformed_type'})
            else:
                selected = body
            record['selected_raw_records'] = deepcopy(selected)
            record['status'] = 'partial_components_recovered_join_unverified' if record['errors'] else 'bytes_authenticated_join_unverified'
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError, OverflowError, RecursionError) as exc:
            record['status'] = 'unavailable_or_malformed'
            record['errors'].append({'exception_type': type(exc).__name__, 'reason': str(exc)[:2048]})
    ledger = sources['saved_ledger']['authenticated_raw_body']
    calls = ledger.get('calls') if type(ledger) is dict else None
    calls_recoverable = type(calls) is list
    return {'all_assigned_execution_audit': {
                'status': 'raw_recovery_shared_join_failed', 'shared_join_verified': False,
                'independent_sources': sources},
            'all_assigned_call_audit': deepcopy(calls) if calls_recoverable else None,
            'all_assigned_call_audit_status': {
                'status': 'bytes_authenticated_join_unverified' if calls_recoverable else 'unavailable_or_malformed',
                'requested_ledger_pin': deepcopy(request.get('saved_ledger')),
                'shared_join_verified': False}}


def _assemble(history, request, docs, adapter, primitives):
    caught = (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError, OverflowError, RecursionError)
    try:
        shared = _global_sources(history, request, docs, adapter)
    except caught as exc:
        error = {'stage': 'shared_source_binding', 'exception_type': type(exc).__name__, 'reason': str(exc)[:2048]}
        rows = [{'source_key': deepcopy(s['source_key']), 'raw_sidecar': deepcopy(s),
                 'raw_provenance': deepcopy(a), 'source_import_ledger': deepcopy(l),
                 'stages': [], 'errors': [deepcopy(error)], 'technical_binding': None,
                 'mechanical_Q_candidate': None, 'reconciled_DEV_value': None,
                 'main_frame_eligible': False, 'main_assigned_weight': None}
                for s, a, l in zip(history['records'], history['raw_provenance'], history['assignment_ledger'])]
        primary, audience, counts = _describe_pairs(rows, history, {}, adapter)
        return {'schema': 'sigres2-historical-limited-dev-description-v1', 'scope': SCOPE,
                'status': 'shared_source_binding_failed', 'errors': [error], 'rows': rows,
                'primary_pairs': primary, 'audience_control_pairs': audience, 'primary_type_counts': counts,
                'historical_population_verified_by_ingestion_only': True,
                'main_frame_eligible': False, 'main_assigned_weights': None, 'main_empirical_estimate': None,
                'human_readings': 0, 'new_actor_calls': 0, 'stronger_replay_qualified': None,
                'source_adoption_or_invocation_authority_verified_here': False,
                **_recover_execution_audit(request, docs)}
    cutoff = adapter._utc(request['analysis_cutoff'])
    require(request['cutoff_contract'] == 'prospective_frozen_before_invocation'
            and request['convention'] == CONVENTION, 'explicit prospective cutoff/convention')
    require(request['frozen_DEV_selection_sha256'] == digest(canonical({
        'owner_scope': request['owner_scope'], 'history_seal_sha256': history['seal_sha256'],
        'worklist_sha256': history['worklist_sha256'], 'all_assigned_selection': request['all_assigned_selection']})),
        'explicit DEV selection digest, not V3 frame')
    consumer = {}
    for row in request['consumer_records']:
        key = _key(row['source_key'])
        require(key not in consumer, 'duplicate consumer source key')
        consumer[key] = row['pin']
    known = {_key(x['source_key']) for x in history['records']}
    require(set(consumer) <= known, 'consumer record outside fixed documentary roster')
    result = []
    for i, (sidecar, aux, ledger) in enumerate(zip(history['records'], history['raw_provenance'], history['assignment_ledger'])):
        out = {'source_key': deepcopy(sidecar['source_key']), 'raw_sidecar': deepcopy(sidecar),
               'raw_provenance': deepcopy(aux), 'source_import_ledger': deepcopy(ledger),
               'stages': [], 'technical_binding': None, 'mechanical_Q_candidate': None,
               'reconciled_DEV_value': None, 'projection': None, 'errors': [],
               'consumer_record_pin': consumer.get(_key(sidecar['source_key'])),
               'frozen_transport_record': deepcopy(shared['table']['rows'][i]),
               'main_frame_eligible': False, 'main_assigned_weight': None}
        result.append(out)
        if ledger['errors']:
            out['errors'].append({'stage': 'historical_source_import', 'source_errors': deepcopy(ledger['errors'])})
            continue
        try:
            binding = _bind_view(sidecar, aux, i, shared, docs, adapter)
            out['technical_binding'] = binding
            out['stages'].append({'stage': 'source_native_literal_binding', 'status': 'verified_saved_bytes'})
        except caught as exc:
            out['errors'].append({'stage': 'source_native_literal_binding', 'exception_type': type(exc).__name__, 'reason': str(exc)[:2048]})
            continue
        if adapter.TYPE_MAP[sidecar['source_key']['source_type']] is None:
            # Audience is a retained secondary control, not an eighth primary
            # awareness construct. Never send these four views to Q/DEV.
            out['primary_P1_type'] = None
            out['secondary_control'] = {'status': 'retained_without_primary_projection',
                                        'raw_summary': deepcopy(sidecar['raw_summary'])}
            out['stages'].append({'stage': 'secondary_audience_control', 'status': 'Q_and_reconciled_DEV_not_applicable'})
            if out['consumer_record_pin'] is not None:
                try:
                    out['unvalidated_secondary_supplied_record'] = docs.json(out['consumer_record_pin'])
                except caught as exc:
                    out['errors'].append({'stage': 'secondary_record_custody', 'exception_type': type(exc).__name__, 'reason': str(exc)[:2048]})
            continue
        out['primary_P1_type'] = adapter.TYPE_MAP[sidecar['source_key']['source_type']]
        if out['consumer_record_pin'] is None:
            out['errors'].append({'stage': 'supplied_owner_decisions', 'reason': 'not_supplied_for_exact_source_key'})
            continue
        try:
            supplied = docs.json(out['consumer_record_pin'])
            # Preserve supplied alternatives/records even when later validation fails.
            out['supplied_consumer_record'] = deepcopy(supplied)
            projected = {'stages': out['stages'], 'active_stage': 'consumer_record_decode'}
            out['projection'] = projected
            _project(sidecar, aux, binding, supplied, out['consumer_record_pin'], request, docs, adapter, primitives, projected)
            out['projection'] = projected
            out['mechanical_Q_candidate'] = projected['mechanical_projection']['value']
            out['reconciled_DEV_value'] = projected['reconciled_DEV']['value']
        except caught as exc:
            partial = out['projection'] or {}
            out['mechanical_Q_candidate'] = partial.get('mechanical_projection', {}).get('value')
            out['errors'].append({'stage': partial.get('active_stage', 'consumer_record_decode'), 'exception_type': type(exc).__name__, 'reason': str(exc)[:2048]})
    primary, audience, counts = _describe_pairs(result, history, shared['table'], adapter)
    return {'schema': 'sigres2-historical-limited-dev-description-v1', 'scope': SCOPE,
            'convention': CONVENTION, 'analysis_cutoff': request['analysis_cutoff'],
            'request_canonical_content_sha256': digest(canonical(request)), 'history_source_seal': history['seal_sha256'],
            'actor_configuration': shared['config'], 'actor_config_pin': shared['config_pin'], 'actor_model_id': shared['actor_id'],
            'all_assigned_execution_audit': deepcopy(shared['table']['all_assigned_execution_audit']),
            'all_assigned_call_audit': deepcopy(shared['ledger']['calls']),
            'rows': result, 'primary_pairs': primary, 'audience_control_pairs': audience, 'primary_type_counts': counts,
            'assigned_views': 32, 'physical_items': 18, 'related_exposed_parents': 2,
            'main_frame_eligible': False, 'main_assigned_weights': None, 'main_empirical_estimate': None,
            'human_readings': 0, 'new_actor_calls': 0, 'stronger_replay_qualified': None,
            'source_adoption_or_invocation_authority_verified_here': False,
            'interpretation': 'Fixed saved-record logical count bounds; no independent-cluster, confidence-interval, causal, latent-awareness or human-validation claim.'}


def describe_historical_dev(packet_root, *, adapter_source, primitives_source,
                            request_bytes, expected_request_sha256, artifact_texts):
    """Future adopted-source entry; caller owns the exact frozen request pin.

    No clocks or cutoff are manufactured here. This is not called on historical
    records during source preparation or inert validation.
    """
    require(type(request_bytes) is bytes and len(request_bytes) <= 8 * 1024**2
            and digest(request_bytes) == expected_request_sha256, 'exact frozen invocation request')
    adapter, primitives = load_sources(adapter_source, primitives_source)
    request = adapter.strict_json(request_bytes)
    require(request['scope'] == SCOPE, 'limited historical scope required')
    docs = Documents(artifact_texts, adapter)
    history = adapter.ingest_history(packet_root, expected_seal_sha256=request['history_seal_sha256'])
    result = _assemble(history, request, docs, adapter, primitives)
    result['original_request_file_pin'] = descriptor(request_bytes)
    return result
