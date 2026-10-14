"""Pure paired views for independently reviewed, complete startup qualifications.

The caller authenticates the aggregate review and all saved inputs. A digest
join here checks those supplied identities; this module grants no authority.
The original initial-only and first-banner adapters remain unchanged.
"""
import copy
import hashlib

from causes import campaign_reader as reader, measurement_view
from causes.grouped_startup_qualification import qualify_run
from causes.neutral_startup_banner_view import _BANNER, _CUE_PATH, _FILES
from causes.neutral_summary_view import build_neutral_pair


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _same(left, right):
    return reader.encoded(left) == reader.encoded(right)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _bank(bank):
    _require(isinstance(bank, dict) and set(bank) == {'definition', 'review'},
             'grouped_view_bank_invalid')
    definition, review = bank['definition'], bank['review']
    _require(isinstance(definition, dict) and set(definition) == {
        'schema', 'members', 'input_members_sha256', 'fixed_membership_preserved',
        'transformed_views_emitted', 'independent_run_review_required'}
        and definition['schema'] == 'causes.grouped_startup_batch.pending.v1'
        and definition['fixed_membership_preserved'] is True
        and definition['transformed_views_emitted'] is False
        and definition['independent_run_review_required'] is True,
        'grouped_view_bank_definition_invalid')
    digest = reader.digest(definition)
    _require(isinstance(review, dict)
             and review.get('accepted_grouped_startup_qualification_batch') is True
             and review.get('definition_sha256') == digest and review.get('findings') == [],
             'grouped_view_bank_review_mismatch')
    rows = definition['members']
    _require(isinstance(rows, list) and rows, 'grouped_view_fixed_members_required')
    indexed = {}
    for row in rows:
        _require(isinstance(row, dict) and {'key', 'qualification', 'reason'} <= set(row)
                 and set(row) <= {'key', 'qualification', 'reason', 'detail'}
                 and isinstance(row['key'], str) and row['key'] and row['key'] not in indexed,
                 'grouped_view_bank_member_invalid')
        proof = row['qualification']
        if proof is None:
            _require(isinstance(row['reason'], str) and row['reason'],
                     'grouped_view_bank_missing_reason_required')
        else:
            _require(isinstance(proof, dict)
                     and proof.get('schema') == 'causes.grouped_startup_qualification.pending.v1'
                     and proof.get('run_id') == row['key'] and row['reason'] is None,
                     'grouped_view_bank_run_mismatch')
        indexed[row['key']] = row
    return indexed, digest


def _build_pair(baseline_episode, treatment_episode, summary_row, construction,
                startup_evidence, captures, dependency_bank, state_bank, row, bank_digest):
    condition = construction['condition_variant']
    _require(condition['id'] == 'F09L03' and _same(condition.get('files'), _FILES)
             and condition.get('env') == ['BASH_ENV=' + _CUE_PATH]
             and condition.get('provenance') == 'experiment_authored_environment_cues_no_hidden_grader_access',
             'grouped_view_construction_unsupported')
    _require(row['key'] == treatment_episode['job']['allocation']['run_id'],
             'grouped_view_run_mismatch')
    _require(row['qualification'] is not None, 'grouped_view_run_not_qualified')
    proof = qualify_run(treatment_episode, startup_evidence, captures, dependency_bank, state_bank)
    _require(_same(proof, row['qualification']), 'grouped_view_regenerated_proof_mismatch')
    _require(proof['all_native_early_invocations_joined'] is True
             and proof['all_injected_startup_banner_intervals_qualified'] is True,
             'grouped_view_incomplete_run_qualification')
    intervals = [item for item in proof['invocations'] if item['qualified']]
    _require(intervals, 'grouped_view_no_qualified_startup_interval')
    _require(len({item['message_index'] for item in intervals}) == len(intervals),
             'grouped_view_duplicate_message_interval')
    views, initial = build_neutral_pair(baseline_episode, treatment_episode, summary_row, construction)
    initial_views = copy.deepcopy(views)
    old_targets = measurement_view.prepare_judge_context(views['treatment'])[1]
    contexts = {}
    for name in ('messages', 'prefix_context'):
        original = treatment_episode[name]
        if original is None:
            continue
        mapping = initial['arms']['treatment']['contexts'][name]['original_to_derived_index']
        edits = []
        for interval in intervals:
            index = interval['message_index']
            if index >= len(original):
                continue
            message = original[index]
            _require(message.get('role') == 'tool'
                     and message.get('tool_call_id') == interval['call_id']
                     and _same(message, treatment_episode['messages'][index]),
                     'grouped_view_original_tool_message_mismatch')
            start, end = interval['start_utf8_byte'], interval['end_utf8_byte']
            raw = message['content'].encode('utf-8')
            limit = treatment_episode['config'].get('tool_output_characters', 32000)
            _require(type(start) is int and type(end) is int and 0 <= start < end <= len(raw)
                     and type(limit) is int and limit >= end
                     and raw[start:end] == _BANNER.encode('utf-8')
                     and interval['raw_stdout_start'] == 0
                     and interval['raw_stdout_end'] == len(_BANNER.encode('utf-8')),
                     'grouped_view_visible_interval_mismatch')
            mapped = mapping[index]
            _require(type(mapped) is int and _same(views['treatment'][name][mapped], message),
                     'grouped_view_initial_index_mismatch')
            replacement = (raw[:start] + raw[end:]).decode('utf-8')
            views['treatment'][name][mapped]['content'] = replacement
            edits.append(dict(call_id=interval['call_id'], original_index=index, derived_index=mapped,
                start_utf8_byte=start, end_utf8_byte=end, removed_text=_BANNER,
                original_message_sha256=reader.digest(message),
                derived_message_sha256=reader.digest(views['treatment'][name][mapped]),
                prefix_sha256=_sha(raw[:start]), suffix_sha256=_sha(raw[end:])))
        restored = copy.deepcopy(views['treatment'][name])
        for edit in reversed(edits):
            message = restored[edit['derived_index']]
            raw = message['content'].encode('utf-8'); start = edit['start_utf8_byte']
            message['content'] = (raw[:start] + edit['removed_text'].encode('utf-8') + raw[start:]).decode('utf-8')
            _require(reader.digest(message) == edit['original_message_sha256'],
                     'grouped_view_message_inverse_failed')
        _require(_same(restored, initial_views['treatment'][name])
                 and _same(treatment_episode['initial_context'] + restored[2:], original),
                 'grouped_view_full_inverse_failed')
        contexts[name] = dict(original_sha256=reader.digest(original),
            derived_sha256=reader.digest(views['treatment'][name]), edits=edits,
            exact_inverse_verified=True)
    _require(_same(old_targets, measurement_view.prepare_judge_context(views['treatment'])[1]),
             'grouped_view_actor_targets_changed')
    _require(_same(views['baseline'], initial_views['baseline']), 'grouped_view_baseline_changed')
    if views['treatment']['prefix_context'] is not None:
        _require(_same(views['treatment']['messages'][:len(views['treatment']['prefix_context'])],
                       views['treatment']['prefix_context']), 'grouped_view_prefix_mismatch')
    return views, dict(schema='causes.neutral_grouped_startup_derivation.v1',
        run_id=row['key'], qualification_bank_sha256=bank_digest,
        qualification_sha256=reader.digest(proof), initial_derivation=initial,
        initial_derivation_stage='before_external_tool_edits', contexts=contexts,
        removed_startup_intervals=len(intervals), all_captured_startup_intervals_qualified=True,
        all_actor_messages_and_arguments_unchanged=True, native_read_bytes_unchanged=True,
        other_tool_bytes_unchanged=True, renderer_truncation_preserved=True,
        baseline_unchanged_from_initial_view=True, exact_inverse_verified=True,
        residual_external_cues_may_remain=True, broader_external_cue_neutrality_established=False)


def build_neutral_pair_with_grouped_startup(baseline_episode, treatment_episode, summary_row,
                                           construction, startup_evidence, captures,
                                           dependency_bank, state_bank, qualification_bank):
    """Derive one complete reviewed run; never fall back to a partially edited view.

    Regenerate the entire original proof before editing. Splice its intervals
    from the saved rendered text, without rendering again or revealing a
    truncated suffix. Native reads and other identical literal text are data.
    """
    rows, digest = _bank(qualification_bank)
    key = treatment_episode['job']['allocation']['run_id']
    _require(key in rows, 'grouped_view_run_not_in_fixed_bank')
    return _build_pair(baseline_episode, treatment_episode, summary_row, construction,
                       startup_evidence, captures, dependency_bank, state_bank, rows[key], digest)


def build_neutral_grouped_batch(members, dependency_bank, state_bank, qualification_bank):
    """Preserve the aggregate bank's complete fixed order and explicit missingness.

    Global bank and membership failures raise before any per-row handling.
    Individual unsupported or unavailable runs retain their key and reason;
    no partial view or replacement member is returned.
    """
    rows, digest = _bank(qualification_bank)
    _require(isinstance(members, list) and all(isinstance(row, dict) for row in members),
             'grouped_view_members_invalid')
    _require([row.get('key') for row in members] == list(rows), 'grouped_view_fixed_membership_mismatch')
    before = reader.digest(members)
    output = []
    fields = {'key', 'baseline_episode', 'treatment_episode', 'summary_row', 'construction',
              'startup_evidence', 'captures'}
    for member in members:
        key = member['key']
        if member.get('unavailable_reason') is not None:
            _require(set(member) == {'key', 'unavailable_reason'}
                     and isinstance(member['unavailable_reason'], str) and member['unavailable_reason'],
                     'grouped_view_unavailable_member_invalid')
            output.append(dict(key=key, views=None, derivation=None,
                reason=member['unavailable_reason'], qualification_reason=rows[key]['reason']))
            continue
        _require(set(member) == fields, 'grouped_view_member_fields_invalid')
        try:
            views, proof = _build_pair(member['baseline_episode'], member['treatment_episode'],
                member['summary_row'], member['construction'], member['startup_evidence'], member['captures'],
                dependency_bank, state_bank, rows[key], digest)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            output.append(dict(key=key, views=None, derivation=None,
                reason='grouped_view_refused', detail=str(error), qualification_reason=rows[key]['reason']))
        else:
            output.append(dict(key=key, views=views, derivation=proof, reason=None, qualification_reason=None))
    _require(reader.digest(members) == before, 'grouped_view_input_mutated')
    return dict(schema='causes.neutral_grouped_startup_batch.v1', members=output,
        qualification_bank_sha256=digest, input_members_sha256=before,
        fixed_membership_preserved=True, unsupported_members_retained=True,
        partial_views_emitted=False, independent_output_review_required=True,
        broader_external_cue_neutrality_established=False)
