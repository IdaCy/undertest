"""Fixed 56-original metadata repeat description; no inference of latent belief or independent reader views."""
from collections import Counter, defaultdict
from fractions import Fraction

IDENTITY = ('actor', 'actor_model', 'actor_role', 'task_id', 'task_code', 'family', 'source_cluster_id', 'core_sha256')

STRATUM = ('question_type', 'method', 'reader_profile', 'reader_model', 'frame', 'target_channel')

ACTION = ('binary_same', 'binary_different', 'repeat1_null_only', 'repeat2_null_only', 'both_null')

LABEL = ('both_present_same', 'both_present_different', 'repeat1_missing_only', 'repeat2_missing_only', 'both_missing')

ENDPOINTS = ('label_state_or_null', 'affirmative_assertion_present_bool_or_null', 'relevant_expression_bool_or_null')

STATES = {'asserted', 'denied', 'mixed', 'uncertain', 'no_relevant_statement'}

def action_relation(first, second):
    a, b = first['task_success_bool_or_null'], second['task_success_bool_or_null']
    assert a is None or type(a) is bool
    assert b is None or type(b) is bool
    if a is None and b is None:
        return 'both_null'
    if a is None:
        return 'repeat1_null_only'
    if b is None:
        return 'repeat2_null_only'
    return 'binary_same' if a == b else 'binary_different'

def label_relation(first, second, endpoint):
    a, b = first['label_present'], second['label_present']
    assert type(a) is bool and type(b) is bool
    if not a and not b:
        return 'both_missing'
    if not a:
        return 'repeat1_missing_only'
    if not b:
        return 'repeat2_missing_only'
    return 'both_present_same' if first[endpoint] == second[endpoint] else 'both_present_different'

def build_repeat_table(rows):
    assert len(rows) == 1568
    grouped = defaultdict(dict)
    originals = {}
    for row in rows:
        identity = tuple(row[k] for k in IDENTITY)
        stratum = tuple(row[k] for k in STRATUM)
        assert row['frame'] == 'current_interaction' and row['target_channel'] == 'public_text'
        assert type(row['repeat']) is int and row['repeat'] in (1, 2)
        assert type(row['label_present']) is bool
        if row['label_present']:
            assert row[ENDPOINTS[0]] in STATES
            assert all(type(row[e]) is bool for e in ENDPOINTS[1:])
        else:
            assert all(row[e] is None for e in ENDPOINTS)
        key = (identity, stratum)
        assert row['repeat'] not in grouped[key]
        grouped[key][row['repeat']] = row
        original = row['original_assignment_id']
        metadata = {k: row[k] for k in (*IDENTITY, 'repeat', 'weight_exact_fraction', 'ACT_status_raw', 'accepted_action_class', 'accepted_null_cause', 'task_success_bool_or_null')}
        if original in originals:
            assert originals[original] == metadata
        originals[original] = metadata
    assert len(originals) == 56 and len(grouped) == 784
    pairs, strata = [], defaultdict(list)
    roster = {}
    for (identity, stratum), components in sorted(grouped.items()):
        assert set(components) == {1, 2}
        first, second = components[1], components[2]
        ids = [first['original_assignment_id'], second['original_assignment_id']]
        assert ids[0] != ids[1]
        assert first['weight_exact_fraction'] == second['weight_exact_fraction']
        weight = Fraction(first['weight_exact_fraction']) + Fraction(second['weight_exact_fraction'])
        if identity in roster:
            assert roster[identity] == ids
        roster[identity] = ids
        relation = action_relation(first, second)
        pair = {'pair_identity': dict(zip(IDENTITY, identity)), 'stratum': dict(zip(STRATUM, stratum)), 'ordered_original_ids': ids, 'components': [first, second], 'pair_weight_exact_fraction': str(weight), 'action_pair_relation': relation, 'label_pair_relations': {e: label_relation(first, second, e) for e in ENDPOINTS}}
        pairs.append(pair)
        strata[stratum].append(pair)
    assert len(roster) == 28 and len(strata) == 28
    assert len({v for ids in roster.values() for v in ids}) == 56
    table, coverage = [], []
    for stratum, group in sorted(strata.items()):
        assert len(group) == 28
        assert {tuple(p['ordered_original_ids']) for p in group} == {tuple(ids) for ids in roster.values()}
        assert sum((Fraction(p['pair_weight_exact_fraction']) for p in group), Fraction()) == 1
        margins = Counter(p['action_pair_relation'] for p in group)
        coverage.append({'stratum': dict(zip(STRATUM, stratum)), 'fixed_pairs': 28, 'fixed_originals': 56, 'action_relation_counts': {k: margins[k] for k in ACTION}, 'label_relation_counts': {e: dict(Counter(p['label_pair_relations'][e] for p in group)) for e in ENDPOINTS}})
        for endpoint in ENDPOINTS:
            cells = []
            for action in ACTION:
                for label in LABEL:
                    members = [p for p in group if p['action_pair_relation'] == action and p['label_pair_relations'][endpoint] == label]
                    cell = {**dict(zip(STRATUM, stratum)), 'endpoint': endpoint, 'action_pair_relation': action, 'label_pair_relation': label, 'pair_count': len(members), 'sum_pair_weight_exact_fraction': str(sum((Fraction(p['pair_weight_exact_fraction']) for p in members), Fraction())), 'fixed_pair_denominator': 28, 'fixed_original_denominator': 56, 'fixed_pair_weight_denominator': '1', 'ordered_original_id_pairs': [p['ordered_original_ids'] for p in members]}
                    cells.append(cell)
            assert sum(c['pair_count'] for c in cells) == 28
            assert sum((Fraction(c['sum_pair_weight_exact_fraction']) for c in cells), Fraction()) == 1
            assert sorted(tuple(ids) for c in cells for ids in c['ordered_original_id_pairs']) == sorted(tuple(ids) for ids in roster.values())
            table.extend(cells)
    assert len(table) == 2100
    scope = {'status': 'ACTUAL_SAVED_METADATA_REPEAT_DESCRIPTION_PENDING_REVIEW', 'unit': 'Recorded actor-task pair, ordered by repeat index only; no chronology claim.', 'fixed_pairs': 28, 'fixed_originals': 56, 'dependent_pair_positions': 784, 'views': 4, 'questions': 7, 'endpoints': list(ENDPOINTS), 'primary_endpoint': ENDPOINTS[0], 'pair_weight_rule': 'sum_of_two_accepted_original_weights', 'missing_and_nulls_remain_separate': True, 'no_available_case_renormalization': True, 'limits': ['Pairing proves saved actor/task/source identities only; matching runtime, requests, seeds, chronology and independence are unestablished.', 'Only two distinct pairs have differing binary action outcomes; question/reader reuse creates no additional independent pairs.', 'Exact label equality is not semantic equivalence or equal private belief; differences cannot separate actor text and reader/instrument variation.', 'Null/null action and missing/missing label are never counted as agreement.', 'No causal, latent-belief, semantic-gold, classifier-performance, actor-winner, population or complete-study claim.'], 'new_labels_or_grades': False, 'runtime_or_body_acquisition': False}
    return {'pairs': pairs, 'table': table, 'coverage': coverage, 'scope': scope}
