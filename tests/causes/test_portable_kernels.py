import hashlib
from pathlib import Path
import tempfile
import unittest

from causes.cue_factors import statistics
from causes.cue_factors.summaries import categories, contrast
from causes.task_contexts.metadata import MetadataError, _pinned_bytes
from causes.layout_labels.analyze import analyze as analyze_layout


class MissingnessTests(unittest.TestCase):
    def test_missing_assignments_keep_full_denominators(self):
        result = categories(['yes', 'no', 'unissued', 'invalid'])
        self.assertEqual(result['assigned'], 4)
        self.assertEqual(result['assigned_mean_identification_bounds'], [.25, .75])
        result = contrast([(0, 1), (None, None)], primary=True)
        self.assertEqual(result['assigned_pairs'], 2)
        self.assertEqual(result['effect_identification_lower'], 0)
        self.assertEqual(result['effect_identification_upper'], 1)
        self.assertEqual(result['p_worst_completion'], 1)

    def test_exact_null_and_holm_family(self):
        self.assertEqual(statistics.paired_randomization_p(0, 0), 1)
        adjusted = statistics.holm_adjust([.001, .04, .3])
        self.assertEqual(adjusted, [.003, .08, .3])


class MetadataPinTests(unittest.TestCase):
    def test_pin_and_rejected_substitutions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / 'source.bin'
            source.write_bytes(b'metadata')
            digest = hashlib.sha256(b'metadata').hexdigest()
            self.assertEqual(_pinned_bytes(str(source), str(source), 8, digest), b'metadata')
            for size, checksum in [(7, digest), (8, '0' * 64)]:
                with self.assertRaises(MetadataError):
                    _pinned_bytes(str(source), str(source), size, checksum)
            alias = directory / 'alias.bin'
            alias.symlink_to(source)
            with self.assertRaises(MetadataError):
                _pinned_bytes(str(alias), str(alias), 8, digest)
            nested = directory / 'nested'
            nested.mkdir()
            (nested / 'data').write_bytes(b'metadata')
            parent_alias = directory / 'parent_alias'
            parent_alias.symlink_to(nested, target_is_directory=True)
            alias_file = str(parent_alias / 'data')
            with self.assertRaises(MetadataError):
                _pinned_bytes(alias_file, alias_file, 8, digest)


class LayoutFrameTests(unittest.TestCase):
    def test_fixed_triples_and_missing_contrasts(self):
        assigned, triples = [], []
        for task in range(6):
            for rendering in ('colon', 'bold'):
                for draw in range(1, 21):
                    identity = f't{task}.{rendering}.D{draw:03}'
                    triple = dict(triple_id=identity, task=f't{task}', rendering=rendering, draw=f'D{draw:03}', round=0, answer_order=len(triples) % 6, view_order=['A', 'B', 'C'])
                    triples.append(triple)
                    for subround, view in enumerate('ABC'):
                        assigned.append({key: value for key, value in triple.items() if key != 'view_order'} | dict(id=f'SL1.{identity}.{view}', view=view, subround=subround, queue_position=len(assigned)))
        manifest = {'schema_version': 'effe2.api_batch.v1', 'rows': [{'id': r['id'], 'metadata': r} for r in assigned]}
        allocation = {'schema': 'effe2.structure_label_allocation.v1', 'rows': assigned, 'triples': triples, 'assigned_calls': 720, 'matched_triples': 240}
        records = [{'id': r['id'], 'metadata': r, 'category': 'unissued', 'status': 'unissued_no_call'} for r in assigned]
        result = analyze_layout(manifest, allocation, records)
        self.assertEqual(result['primary_test_family_size'], 2)
        self.assertTrue(all(r['holm_adjusted_p'] == 1 for r in result['primary_contrasts']))
        self.assertEqual(result['overall']['assigned_mean_identification_bounds'], [0, 1])
        with self.assertRaises(ValueError):
            analyze_layout(manifest, allocation, records[:-1])
