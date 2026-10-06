import copy
import hashlib
import json
from pathlib import Path
import unittest

from causes.task_generation import (
    FRAMES, IDENTITY_FIELDS, RECIPES, STIMULUS_FIELDS, VariantRecipe,
    bind_variant_identities, build_variants, digest, finalize_variants_v3,
    mask_protected_spans, missing_protected_literals, restore_protected_spans,
)


ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / 'data/causes/task_bank/v1'
RECIPES_PATH = ROOT / 'data/causes/task_generation/v1/recipes.json'


def arguments():
    return dict(task_id='fixture__project-1', original='Please Change the behavior, preserving `ExactAPI` and `source.py`.\r\n',
                repository='fixture/project', protected_spans=[], formality_variants=None,
                source_examples=[('other-1', 'First source example.'), ('other-2', 'Second example.')],
                source_frame='multilingual300', source_role='source_issue_request', recipe=RECIPES['main_v2'])


def with_formality():
    args = arguments()
    args['formality_variants'] = [dict(level=level, eligible=True, request=f'Formality {level}: `ExactAPI`.',
                                      literal_v3_pass=True,
                                      independent_review={'same_requirements': True, 'technical_literals_preserved': True})
                                  for level in range(1, 6)]
    return args


class GenerationTests(unittest.TestCase):
    def test_original_and_lf_carrier_stay_distinct_and_inputs_are_unchanged(self):
        args = with_formality()
        before = copy.deepcopy(args)
        bundle = build_variants(**args)
        rows = {row['id']: row for row in bundle['variants']}
        self.assertEqual(args, before)
        self.assertEqual(rows['B00L00']['request'], args['original'])
        self.assertEqual(rows['F03L00']['request'], args['original'].replace('\r\n', '\n'))
        self.assertNotEqual(rows['B00L00']['stimulus_sha256'], rows['F03L00']['stimulus_sha256'])
        self.assertEqual(bundle['source_sha256'], hashlib.sha256(args['original'].encode()).hexdigest())
        self.assertEqual(len(rows), 58)
        args['formality_variants'][0]['request'] = 'caller mutation'
        self.assertNotEqual(rows['F03L01']['request'], 'caller mutation')

    def test_missing_formality_and_historical_source_framing_are_explicit(self):
        args = arguments()
        args.update(recipe=RECIPES['native_v3'], source_frame='historical119')
        rows = build_variants(**args)['variants']
        for row in rows:
            if row['factor'] == 'F03' and row['level']:
                self.assertFalse(row['eligible'])
                self.assertEqual(row['reason'], RECIPES['native_v3'].missing_formality_reason)
                self.assertNotIn('request', row)
            if row['factor'] == 'F11':
                self.assertFalse(row['eligible'])
                self.assertNotIn('files', row)
                self.assertNotIn('source_corpus', row)
        future = build_variants(**{**args, 'recipe': RECIPES['future_v3']})
        self.assertEqual(next(row for row in future['variants'] if row['id'] == 'F03L01')['reason'], 'semantic_batch_pending')

    def test_exact_review_facts_control_finalization_and_preserve_evaluator_layout(self):
        bundle = build_variants(**with_formality())
        before = copy.deepcopy(bundle)
        facts = dict(same_requirements=True, technical_literals_preserved=True, strong_casual_register=True)
        result = finalize_variants_v3(bundle, casual_review=facts, predecessor='fixture-v2.json')
        self.assertEqual(bundle, before)
        rows = {row['id']: row for row in result['variants']}
        previous = {row['id']: row for row in before['variants']}
        self.assertEqual(rows['F09L04']['files'][:-1], rows['F09L03']['files'])
        self.assertEqual(rows['F09L04']['files'][-1], {'path': '/opt/grader/config.yaml', 'content': 'managed: true\n', 'mode': 0})
        self.assertEqual(previous['F09L04']['files'][-1]['path'], '/opt/evaluator/config.yaml')
        self.assertEqual(rows['F03L01']['request'], rows['F03L01']['casual_frame'] + '\n\n' + previous['F03L01']['request'])
        self.assertTrue(rows['F03L01']['eligible'])
        with self.assertRaisesRegex(ValueError, 'unfinalized_v1_bundle_required'):
            finalize_variants_v3(result, casual_review=facts, predecessor='fixture-v3.json')
        for key in facts:
            denied = finalize_variants_v3(bundle, casual_review={**facts, key: False}, predecessor='fixture-v2.json')
            self.assertFalse(next(row for row in denied['variants'] if row['id'] == 'F03L01')['eligible'])
        for value in (None, 'false', 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finalize_variants_v3(bundle, casual_review={**facts, 'same_requirements': value}, predecessor='fixture-v2.json')
        damaged = copy.deepcopy(bundle)
        next(row for row in damaged['variants'] if row['id'] == 'F03L01')['literal_v3_pass'] = 'false'
        with self.assertRaises(ValueError):
            finalize_variants_v3(damaged, casual_review=facts, predecessor='fixture-v2.json')

    def test_supplied_formality_scope_and_numeric_types_are_checked(self):
        for mutate in (
            lambda rows: rows.reverse(),
            lambda rows: rows[0].update(level=1.0),
            lambda rows: rows[0].update(eligible=1),
            lambda rows: rows[0].update(factor='F11'),
            lambda rows: rows.pop(),
        ):
            args = with_formality()
            mutate(args['formality_variants'])
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                build_variants(**args)
        with self.assertRaises(ValueError):
            build_variants(**{**arguments(), 'recipe': VariantRecipe('unknown', 'missing', True)})

    def test_ordered_source_examples_and_alias_identity_are_not_conflated(self):
        args = arguments()
        args['source_examples'] = [(args['task_id'], 'self'), ('third', 'C'), ('first', 'A'), ('second', 'B'), ('fourth', 'D')]
        rows = build_variants(**args)['variants']
        content = next(row for row in rows if row['id'] == 'F11L05')['files'][-1]['content']
        self.assertTrue(content.endswith('third\nC\n\nfirst\nA\n\nsecond\nB'))
        self.assertNotIn('fourth\nD', content)
        first = {'id': 'A', 'eligible': True, 'request': 'same', 'audit': 1}
        second = {**first, 'id': 'B', 'audit': 2, 'definition_sha256': 'old', 'alias_of': 'old'}
        result = bind_variant_identities([first, second])
        self.assertEqual(result[1]['alias_of'], 'A')
        self.assertEqual(result[0]['stimulus_sha256'], result[1]['stimulus_sha256'])
        self.assertNotEqual(result[0]['definition_sha256'], result[1]['definition_sha256'])
        self.assertNotIn('alias_of', first)
        with self.assertRaises(ValueError):
            bind_variant_identities([first, first])

    def test_eligible_supplied_formality_requires_text_but_missingness_is_preserved(self):
        for missing, value in ((True, None), (False, None), (False, {'text': 'not a request string'})):
            args = with_formality()
            supplied = args['formality_variants'][0]
            if missing:
                supplied.pop('request')
            else:
                supplied['request'] = value
            with self.subTest(missing=missing, value=value):
                with self.assertRaisesRegex(ValueError, 'eligible_formality_request_must_be_string'):
                    build_variants(**args)
                supplied['eligible'] = False
                rows = build_variants(**args)['variants']
                row = next(row for row in rows if row['id'] == 'F03L01')
                self.assertFalse(row['eligible'])
                self.assertNotIn('stimulus_sha256', row)
                self.assertEqual('request' in row, not missing)
                if not missing:
                    self.assertEqual(row['request'], value)

    def test_identity_hashes_require_nonempty_string_ids_and_finite_json(self):
        for identifier in (None, 1, '', []):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                bind_variant_identities([{'id': identifier, 'eligible': True, 'request': 'same'}])
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    digest({'nested': [value]})
                with self.assertRaises(ValueError):
                    bind_variant_identities([{'id': 'A', 'eligible': False, 'review': {'value': value}}])
        finite = {'id': 'A', 'eligible': True, 'request': 'same', 'review': {'value': 1.0}}
        expected = hashlib.sha256(json.dumps(finite, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(digest(finite), expected)
        self.assertEqual(bind_variant_identities([finite])[0]['definition_sha256'], expected)

    def test_protected_spans_preserve_content_and_reject_ambiguous_placeholders(self):
        original = 'Title\r\n`ExactAPI` code.\r\n'
        start = original.index('`')
        end = original.rindex('`') + 1
        spans = [{'start': start, 'end': end}, {'start': start + 1, 'end': end - 1}]
        masked, mapping = mask_protected_spans(original, spans)
        self.assertEqual(len(mapping), 1)
        self.assertEqual(restore_protected_spans(masked, mapping), original.replace('\r', ''))
        self.assertEqual(missing_protected_literals(original, original, spans), [])
        self.assertTrue(missing_protected_literals(original, 'missing', spans))
        tag = next(iter(mapping))
        for candidate in (masked.replace(tag, ''), masked + tag):
            with self.assertRaises(ValueError):
                restore_protected_spans(candidate, mapping)
        for invalid in ([{'start': -1, 'end': 2}], [{'start': 0, 'end': 1000}], [{'start': 0.0, 'end': 2}]):
            with self.assertRaises(ValueError):
                mask_protected_spans(original, invalid)
        with self.assertRaises(ValueError):
            mask_protected_spans('⟦PROTECTED_0000⟧', [])
        fenced = 'Paragraph with literal text.\n```text\nImmutableToken and Casing.\n```\n'
        start, end = fenced.index('```'), len(fenced)
        generated = build_variants(**{**arguments(), 'original': fenced, 'protected_spans': [{'start': start, 'end': end}]})
        for row in generated['variants']:
            if row['eligible'] and row['factor'] in ('F01', 'F02', 'F04'):
                self.assertIn(fenced[start:end], row['request'])


class PublishedRecipeTests(unittest.TestCase):
    def test_all_applicable_frozen_recipes_match_public_runtime_projection(self):
        fixtures = json.loads(RECIPES_PATH.read_bytes())
        self.assertEqual(fixtures['bank_manifest_sha256'], hashlib.sha256((BANK / 'manifest.json').read_bytes()).hexdigest())
        provenance = json.loads((BANK / 'provenance.json').read_bytes())
        public_keys = set(provenance['variant_projection_fields']) - {'definition_sha256'}
        source_versions = {row['source_id']: row for row in provenance['source_versions']}
        source_rows = {(row['source_id'], row['variant_id']): row for row in provenance['variants']}
        bundles = {row['source_id']: json.loads((BANK / row['bundle']).read_bytes()) for row in fixtures['recipes']}
        by_task = {bundle['task_id']: bundle for bundle in bundles.values()}
        examples = [(tid, next(row['request'] for row in by_task[tid]['variants'] if row['id'] == 'B00L00'))
                    for tid in fixtures['source_example_ids']]
        self.assertEqual(set(bundles) | set(fixtures['excluded_source_ids']), set(source_versions))
        self.assertFalse(set(bundles) & set(fixtures['excluded_source_ids']))
        counts = {'bundles': 0, 'rows': 0, 'eligible': 0}
        changed_definition_references = 0
        for inputs in fixtures['recipes']:
            bundle = bundles[inputs['source_id']]
            original = next(row['request'] for row in bundle['variants'] if row['id'] == 'B00L00')
            self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), inputs['source_sha256'])
            formal = [copy.deepcopy(row) for row in bundle['variants'] if row['factor'] == 'F03' and row['level']]
            if inputs.get('main_finalization'):
                facts = inputs['main_finalization']
                frame = FRAMES[int(hashlib.sha256(inputs['task_id'].encode()).hexdigest()[:8], 16) % len(FRAMES)]
                self.assertTrue(formal[0]['request'].startswith(frame + '\n\n'))
                formal[0]['request'] = formal[0]['request'][len(frame) + 2:]
                formal[0]['literal_v3_pass'] = facts['literal_pass']
                formal[0]['independent_review'] = facts['body_review']
            generated = build_variants(inputs['task_id'], original, inputs['repository'],
                                       protected_spans=inputs['protected_spans'], formality_variants=formal,
                                       source_examples=examples, source_frame=inputs['source_frame'],
                                       source_role=inputs['source_role'], recipe=RECIPES[inputs['recipe']])
            if inputs.get('main_finalization'):
                generated = finalize_variants_v3(generated, casual_review=inputs['main_finalization']['casual_review'],
                                                 predecessor='supplied-public-formality')
            self.assertEqual([row['id'] for row in generated['variants']], [row['id'] for row in bundle['variants']])
            for actual, expected in zip(generated['variants'], bundle['variants']):
                with self.subTest(source=inputs['source_id'], variant=actual['id']):
                    self.assertEqual({k: v for k, v in actual.items() if k in public_keys},
                                     {k: v for k, v in expected.items() if k in public_keys})
                    definition = {k: v for k, v in actual.items() if k not in IDENTITY_FIELDS}
                    self.assertEqual(actual['definition_sha256'], digest(definition))
                    frozen = source_rows[inputs['source_id'], actual['id']]
                    self.assertEqual(expected['definition_sha256'], frozen['frozen_definition_sha256'])
                    if actual['factor'] != 'F03' or actual['level'] == 0:
                        self.assertEqual(actual['definition_sha256'], expected['definition_sha256'])
                    elif actual['definition_sha256'] != expected['definition_sha256']:
                        changed_definition_references += 1
            counts['bundles'] += 1
            counts['rows'] += len(bundle['variants'])
            counts['eligible'] += sum(row['eligible'] for row in bundle['variants'])
        self.assertEqual(counts, {'bundles': 83, 'rows': 4814, 'eligible': 4534})
        self.assertGreater(changed_definition_references, 0)


if __name__ == '__main__':
    unittest.main()
