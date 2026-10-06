import contextlib
import copy
import json
import sys
import types
import unittest
from unittest.mock import patch

import causes.recipe_controls.task_runner_metadata as target
from causes.task_contexts.metadata import MetadataError


def row(index=0):
    return {
        "repo": "Org/Repo", "instance_id": "row-" + str(index),
        "base_commit": "a" * 40, "image": "registry/image:tag", "log_parser": "parser label",
    }


def identities(rows):
    return [{name: item[name] for name in target.IDENTITY_FIELDS} for item in rows]


class Protected:
    def __str__(self):
        raise AssertionError("excluded value rendered")

    def __repr__(self):
        raise AssertionError("excluded value rendered")


class FakeParquet:
    def __init__(self, rows, optional=("image", "log_parser")):
        self.rows = rows
        self.present = list(target.IDENTITY_FIELDS) + list(optional)
        self.leaves = self.present + ["problem_statement", "patch", "test_patch", "eval_script", "F2P", "P2P"]
        self.types = {name: "string" for name in self.present}
        self.nullable = {name: True for name in self.present}
        self.paths = {name: name for name in self.present}
        self.sizes = {name: 1 for name in self.present}
        self.num_rows = len(rows)
        self.num_columns = len(self.leaves)
        self.num_row_groups = 1
        self.serialized_size = 100
        self.events = []
        self.table_rows = None
        self.table_columns = None
        self.table_num_rows = None
        self.failure = None

    @property
    def metadata(self):
        return self

    @property
    def schema(self):
        return types.SimpleNamespace(names=list(self.leaves))

    @property
    def schema_arrow(self):
        return types.SimpleNamespace(names=list(self.present), field=self.field)

    def field(self, name):
        if name not in target.FIELDS:
            raise AssertionError("excluded schema field accessed")
        return types.SimpleNamespace(type=self.types[name], nullable=self.nullable[name])

    def row_group(self, index):
        if index != 0:
            raise AssertionError("unexpected row group")
        return self

    def column(self, index):
        name = self.leaves[index]
        if name not in target.FIELDS:
            raise AssertionError("excluded column metadata accessed")
        self.events.append(("column", name))
        return types.SimpleNamespace(
            path_in_schema=self.paths[name], total_uncompressed_size=self.sizes[name]
        )

    def read(self, *, columns, use_threads, use_pandas_metadata):
        self.events.append(("read", tuple(columns), use_threads, use_pandas_metadata))
        if columns != self.present or use_threads is not False or use_pandas_metadata is not False:
            raise AssertionError("projection contract changed")
        if self.failure:
            raise self.failure
        records = [{name: item[name] for name in columns} for item in self.rows]
        return types.SimpleNamespace(
            num_rows=self.num_rows if self.table_num_rows is None else self.table_num_rows,
            column_names=columns if self.table_columns is None else self.table_columns,
            to_pylist=lambda: records if self.table_rows is None else self.table_rows,
        )

    def __enter__(self):
        self.events.append(("enter",))
        return self

    def __exit__(self, *args):
        self.events.append(("exit",))
        return False


@contextlib.contextmanager
def fake_arrow(parquet, pin_hook=None):
    calls = []
    payload = b"verified inert buffer"
    pa = types.ModuleType("pyarrow")
    pq = types.ModuleType("pyarrow.parquet")
    pa.parquet = pq
    pa.types = types.SimpleNamespace(
        is_string=lambda value: value == "string", is_null=lambda value: value == "null"
    )

    def buffer(content):
        if content is not payload:
            raise AssertionError("parser did not receive exact verified buffer")
        calls.append(("buffer",))
        return content

    def factory(content, **kwargs):
        if content is not payload or kwargs != {
            "thrift_string_size_limit": 65536,
            "thrift_container_size_limit": 4096,
            "pre_buffer": False,
        }:
            raise AssertionError("parser constructor contract changed")
        calls.append(("parquet",))
        return parquet

    def pinned(*args):
        calls.append(("pin", args))
        if pin_hook:
            pin_hook()
        return payload

    pa.BufferReader = buffer
    pq.ParquetFile = factory
    with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
        with patch.object(target, "_pinned_bytes", side_effect=pinned):
            yield calls


def read_fake(selected):
    return target.read_pinned_parquet_runner_metadata(
        "/selected/source.parquet", selected_path="/selected/source.parquet",
        expected_size=19, expected_sha256="a" * 64, selected=selected,
    )


class ProjectRunnerMetadataTests(unittest.TestCase):
    def safe_failure(self, records, selected):
        with self.assertRaises(MetadataError) as raised:
            target.project_runner_metadata(records, selected=selected)
        self.assertLess(len(str(raised.exception)), 120)
        self.assertNotIn("SECRET", str(raised.exception))

    def test_order_exact_join_and_no_mutation(self):
        records = [row(i) for i in range(4)]
        before = copy.deepcopy(records)
        selected = identities([records[3], records[1]])
        result = target.project_runner_metadata(records, selected=selected)
        self.assertEqual(result, [records[3], records[1]])
        self.assertEqual(records, before)
        self.assertEqual(result, target.project_runner_metadata(tuple(reversed(records)), selected=tuple(selected)))
        self.assertEqual([tuple(item) for item in result], [target.FIELDS] * 2)
        records[3]["image"] = "changed"
        self.assertNotEqual(result[0]["image"], "changed")

    def test_missing_optional_and_nulls_remain_unknown(self):
        for absent in ((), ("image",), ("log_parser",), target.OPTIONAL_FIELDS):
            source = row()
            for key in absent:
                del source[key]
            for key in target.OPTIONAL_FIELDS:
                if key in source:
                    source[key] = None
            result = target.project_runner_metadata([source], selected=identities([source]))
            self.assertEqual(result[0]["image"], None)
            self.assertEqual(result[0]["log_parser"], None)
            self.assertEqual(set(result[0]), set(target.FIELDS))

    def test_metadata_labels_are_not_executable_validation(self):
        source = row()
        source.update(image=" unverified image label ", log_parser=" unverified parser ")
        self.assertEqual(
            target.project_runner_metadata([source], selected=identities([source])), [source]
        )

    def test_selection_must_be_bounded_exact_builtin_identities(self):
        class ListSubclass(list):
            pass
        class DictSubclass(dict):
            pass
        valid = identities([row()])
        for selected in (
            None, [], iter(valid), ListSubclass(valid), [None], [DictSubclass(valid[0])],
            [dict(valid[0], extra=Protected())], [dict(valid[0], repo=Protected())],
            [{name: value for name, value in valid[0].items() if name != "repo"}],
            identities([row(i) for i in range(8)]),
        ):
            with self.subTest(kind=type(selected).__name__):
                self.safe_failure([row()], selected)

    def test_bad_identity_values(self):
        invalid = {
            "repo": ["bad", "/repo", "a/b/c", "a/..", "Org/SECRET\n"],
            "instance_id": ["bad/id", "SECRET space", "x" * 257],
            "base_commit": ["g" * 40, "a" * 39, "a" * 41],
        }
        for name, values in invalid.items():
            for value in values:
                selected = identities([row()])
                selected[0][name] = value
                self.safe_failure([row()], selected)

    def test_duplicate_missing_and_exact_source_join_fail(self):
        source = [row(0), row(1), row(2)]
        self.safe_failure(source, identities([source[0], source[0]]))
        self.safe_failure(source + [dict(row(2), repo="Other/Repo")], identities([row()]))
        self.safe_failure(source, identities([row(9)]))
        self.safe_failure([], identities([row()]))
        for field, value in (("repo", "org/Repo"), ("base_commit", "A" * 40)):
            selected = identities([row()])
            selected[0][field] = value
            self.safe_failure(source, selected)

    def test_excluded_values_and_types_are_not_rendered(self):
        source = row()
        for key in ("problem_statement", "patch", "test_patch", "eval_script", "F2P", "P2P", "SECRET"):
            self.safe_failure([dict(source, **{key: Protected()})], identities([source]))
        self.safe_failure([{**source, 2: Protected()}], identities([source]))
        for bad in (None, iter([source]), [None], [Protected()]):
            self.safe_failure(bad, identities([source]))
        for field in target.IDENTITY_FIELDS:
            missing = dict(source)
            del missing[field]
            self.safe_failure([missing], identities([source]))

    def test_plain_bounded_strings(self):
        class StringSubclass(str):
            pass
        for field, maximum in zip(target.FIELDS, (201, 256, 40, 512, 128)):
            for value in (True, 4, {}, [], Protected(), StringSubclass("label"),
                          "", " ", "SECRET\0", "SECRET\x7f", "SECRET\u00e9",
                          "x" * (maximum + 1)):
                source = row()
                source[field] = value
                self.safe_failure([source], identities([row()]))
        for field in target.IDENTITY_FIELDS:
            source = row()
            source[field] = None
            self.safe_failure([source], identities([row()]))

    def test_row_and_output_bounds_are_real(self):
        records = [row(i) for i in range(300)]
        selected = identities(records[:7])
        self.assertEqual(len(target.project_runner_metadata(records, selected=selected)), 7)
        self.safe_failure(records + [row(300)], selected)
        large = [
            dict(row(i), repo="a" * 100 + "/" + "b" * 100,
                 instance_id="x" * 253 + format(i, "03d"),
                 image="\\" * 512, log_parser="\\" * 128)
            for i in range(7)
        ]
        result = target.project_runner_metadata(large, selected=identities(large))
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        self.assertLess(len(encoded), 16384)
        self.assertLessEqual(len(encoded), target.MAX_OUTPUT_BYTES)
        with patch.object(target, "MAX_OUTPUT_BYTES", len(encoded) - 1):
            self.safe_failure(large, identities(large))
        with patch.object(target, "MAX_OUTPUT_BYTES", len(encoded)):
            self.assertEqual(target.project_runner_metadata(large, selected=identities(large)), result)



class ReadRunnerMetadataTests(unittest.TestCase):
    def assert_read_failure(self, parquet, selected=None):
        with fake_arrow(parquet):
            with self.assertRaises(MetadataError) as raised:
                read_fake(identities([row()]) if selected is None else selected)
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 120)

    def test_only_projected_columns_and_exact_buffer_single_thread(self):
        records = [dict(row(i), problem_statement=Protected(), patch=Protected(), test_patch=Protected(), eval_script=Protected(), F2P=Protected(), P2P=Protected()) for i in range(3)]
        parquet = FakeParquet(records)
        selected = identities([records[2], records[0]])
        with fake_arrow(parquet) as calls:
            result = read_fake(selected)
        self.assertEqual([x["instance_id"] for x in result], ["row-2", "row-0"])
        self.assertEqual(calls, [
            ("pin", ("/selected/source.parquet", "/selected/source.parquet", 19, "a" * 64)),
            ("buffer",), ("parquet",),
        ])
        self.assertEqual([event for event in parquet.events if event[0] == "read"],
                         [("read", target.FIELDS, False, False)])
        self.assertEqual([event[1] for event in parquet.events if event[0] == "column"], list(target.FIELDS))
        self.assertEqual(parquet.events[-1], ("exit",))

    def test_selection_rejected_before_source_or_parser(self):
        for selected in ([], [dict(identities([row()])[0], base_commit="SECRET")]):
            with patch.object(target, "_pinned_bytes", side_effect=AssertionError("read too early")) as pinned:
                with self.assertRaises(MetadataError):
                    read_fake(selected)
                pinned.assert_not_called()

    def test_selection_frozen_before_pin_read(self):
        selected = identities([row()])
        parquet = FakeParquet([row()])
        with fake_arrow(parquet, lambda: selected[0].update(repo="Changed/Repo")):
            result = read_fake(selected)
        self.assertEqual(result[0]["repo"], "Org/Repo")
        self.assertEqual(selected[0]["repo"], "Changed/Repo")

    def test_original_pin_failure_propagates_without_parser(self):
        failure = MetadataError("source pin mismatch")
        with patch.object(target, "_pinned_bytes", side_effect=failure):
            with self.assertRaises(MetadataError) as raised:
                read_fake(identities([row()]))
        self.assertIs(raised.exception, failure)

    def test_optional_columns_absent_or_null_typed(self):
        for optional in ((), ("image",), ("log_parser",), target.OPTIONAL_FIELDS):
            source = row()
            for field in target.OPTIONAL_FIELDS:
                source[field] = None
            parquet = FakeParquet([source], optional)
            for field in optional:
                parquet.types[field] = "null"
            with fake_arrow(parquet):
                result = read_fake(identities([source]))
            self.assertEqual(result, [source])
            self.assertEqual([e for e in parquet.events if e[0] == "read"],
                             [("read", tuple(target.IDENTITY_FIELDS) + tuple(optional), False, False)])

    def test_schema_nullability_is_enforced(self):
        for field in target.OPTIONAL_FIELDS:
            source = row()
            source[field] = None
            parquet = FakeParquet([source])
            parquet.nullable[field] = False
            self.assert_read_failure(parquet)
            parquet.types[field] = "null"
            self.assert_read_failure(parquet)

    def test_schema_type_and_flat_identity_boundaries(self):
        for field in target.FIELDS:
            parquet = FakeParquet([row()])
            parquet.types[field] = "list"
            self.assert_read_failure(parquet)
            self.assertFalse(any(e[0] == "read" for e in parquet.events))
        for field in target.IDENTITY_FIELDS:
            parquet = FakeParquet([row()])
            parquet.present.remove(field)
            parquet.leaves.remove(field)
            parquet.num_columns -= 1
            self.assert_read_failure(parquet)
        parquet = FakeParquet([row()])
        parquet.leaves.append("image")
        parquet.num_columns += 1
        self.assert_read_failure(parquet)
        parquet = FakeParquet([row()])
        parquet.leaves.remove("image")
        parquet.num_columns -= 1
        self.assert_read_failure(parquet)
        parquet = FakeParquet([row()])
        parquet.paths["repo"] = "nested.repo"
        self.assert_read_failure(parquet)

    def test_schema_scalar_bounds_fail_before_column_decode(self):
        for name, values in {
            "num_rows": (-1, 301, True, 1.0),
            "num_columns": (0, 65, True, 5.0),
            "num_row_groups": (-1, 301, True, 1.0),
            "serialized_size": (-1, 65537, True, 3.0),
        }.items():
            for value in values:
                parquet = FakeParquet([row()])
                setattr(parquet, name, value)
                self.assert_read_failure(parquet)
                self.assertFalse(any(e[0] == "read" for e in parquet.events))

    def test_declared_projected_byte_sum_and_paths(self):
        parquet = FakeParquet([row()])
        parquet.sizes = {name: 0 for name in target.FIELDS}
        parquet.sizes["repo"] = 2097152
        with fake_arrow(parquet):
            self.assertEqual(read_fake(identities([row()])), [row()])
        parquet.sizes["image"] = 1
        self.assert_read_failure(parquet)
        for value in (-1, True, 2097153):
            parquet = FakeParquet([row()])
            parquet.sizes["image"] = value
            self.assert_read_failure(parquet)

    def test_projected_table_shape_and_decoded_records_are_checked(self):
        for attribute, value in (
            ("table_num_rows", 2), ("table_columns", ["problem_statement"]),
            ("table_rows", []), ("table_rows", (row(),)),
            ("table_rows", [dict(row(), problem_statement=Protected())]),
        ):
            parquet = FakeParquet([row()])
            setattr(parquet, attribute, value)
            self.assert_read_failure(parquet)
        parquet = FakeParquet([row()])
        parquet.table_rows = [dict(row(), base_commit="b" * 40)]
        self.assert_read_failure(parquet)

    def test_unselected_duplicate_ids_fail_after_one_read(self):
        parquet = FakeParquet([row(), row(1), row(1)])
        self.assert_read_failure(parquet)
        self.assertEqual(sum(e[0] == "read" for e in parquet.events), 1)

    def test_parser_failure_message_is_not_exported(self):
        parquet = FakeParquet([row()])
        parquet.failure = ValueError("SECRET protected parser details")
        self.assert_read_failure(parquet)
        self.assertEqual(parquet.events[-1], ("exit",))


if __name__ == "__main__":
    unittest.main()
