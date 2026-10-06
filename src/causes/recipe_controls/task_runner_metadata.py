"""Selected runner metadata only; labels are not executable/scoring approval."""

import json
import re

from causes.task_contexts.metadata import MetadataError, _pinned_bytes

FIELDS = ("repo", "instance_id", "base_commit", "image", "log_parser")
IDENTITY_FIELDS = FIELDS[:3]
OPTIONAL_FIELDS = FIELDS[3:]
MAX_ROWS = 300
MAX_SELECTED = 7
MAX_PROJECTED_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 65536
_LENGTHS = dict(zip(FIELDS, (201, 256, 40, 512, 128)))
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


def _keys(row):
    if type(row) is not dict or len(row) > len(FIELDS) or any(type(key) is not str for key in row):
        raise MetadataError("invalid runner record type")


def _text(value, name):
    if type(value) is not str or not 1 <= len(value) <= _LENGTHS[name]:
        raise MetadataError("invalid runner field=" + name)
    minimum = 32 if name in OPTIONAL_FIELDS else 33
    if not all(minimum <= ord(char) <= 126 for char in value) or not value.strip(" "):
        raise MetadataError("invalid runner field=" + name)
    return value


def _identity(row):
    values = tuple(_text(row[name], name) for name in IDENTITY_FIELDS)
    if not _REPO.fullmatch(values[0]):
        raise MetadataError("invalid runner field=repo")
    if not _ID.fullmatch(values[1]):
        raise MetadataError("invalid runner field=instance_id")
    if not _COMMIT.fullmatch(values[2]):
        raise MetadataError("invalid runner field=base_commit")
    return values


def _selection(selected):
    if type(selected) not in (list, tuple) or not 1 <= len(selected) <= MAX_SELECTED:
        raise MetadataError("invalid runner selection count or type")
    frozen = []
    seen = set()
    for row in selected:
        _keys(row)
        if set(row) != set(IDENTITY_FIELDS):
            raise MetadataError("invalid runner selection fields")
        identity = _identity(row)
        if identity[1] in seen:
            raise MetadataError("duplicate runner selection ID")
        seen.add(identity[1])
        frozen.append(identity)
    return tuple(frozen)


def _project(records, selected, nullable=OPTIONAL_FIELDS):
    if type(records) not in (list, tuple) or len(records) > MAX_ROWS:
        raise MetadataError("invalid runner records count or type")
    by_id = {}
    for row in records:
        _keys(row)
        if any(name not in row for name in IDENTITY_FIELDS):
            raise MetadataError("missing runner identity fields")
        if any(name not in FIELDS for name in row):
            raise MetadataError("unexpected runner fields")
        identity = _identity(row)
        if identity[1] in by_id:
            raise MetadataError("duplicate source runner ID")
        values = dict(zip(IDENTITY_FIELDS, identity))
        for name in OPTIONAL_FIELDS:
            value = row.get(name)
            if value is None:
                if name not in nullable:
                    raise MetadataError("invalid null runner field=" + name)
                values[name] = None
            else:
                values[name] = _text(value, name)
        by_id[identity[1]] = values
    result = []
    for identity in selected:
        row = by_id.get(identity[1])
        if row is None:
            raise MetadataError("selected runner ID missing")
        if tuple(row[name] for name in IDENTITY_FIELDS) != identity:
            raise MetadataError("selected runner identity mismatch")
        result.append(row)
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise MetadataError("runner output bytes exceeds 65536")
    return result


def project_runner_metadata(records, *, selected):
    """Join 1–7 exact identity dictionaries; return only five-key rows in selected order.

    Plain preprojected records may omit optional labels or contain None. Those
    become unknown (None); strings are retained exactly without execution checks.
    The Parquet entry point additionally checks optional-column schema nullability.
    """
    return _project(records, _selection(selected))


def _bounded_integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def read_pinned_parquet_runner_metadata(
    path, *, selected_path, expected_size, expected_sha256, selected
):
    """Project only the fixed five-column allowlist from a separately selected object.

    Selection is validated and copied before source reading or lazy parser import.
    Source/package/native resources and metadata exposure remain caller-selected.
    Missing optional schema columns are not read and become None in the result.
    Declared byte bounds are admission checks, not native allocation guarantees.
    """
    frozen = _selection(selected)
    content = _pinned_bytes(path, selected_path, expected_size, expected_sha256)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        with pq.ParquetFile(
            pa.BufferReader(content),
            thrift_string_size_limit=65536,
            thrift_container_size_limit=4096,
            pre_buffer=False,
        ) as parquet:
            metadata = parquet.metadata
            if not _bounded_integer(metadata.num_rows, 0, MAX_ROWS):
                raise MetadataError("invalid runner metadata rows")
            if not _bounded_integer(metadata.num_columns, 1, 64):
                raise MetadataError("invalid runner metadata columns")
            if not _bounded_integer(metadata.num_row_groups, 0, MAX_ROWS):
                raise MetadataError("invalid runner metadata groups")
            if not _bounded_integer(metadata.serialized_size, 0, 65536):
                raise MetadataError("invalid runner schema bytes")
            leaf_names = parquet.schema.names
            schema = parquet.schema_arrow
            names = schema.names
            if (
                type(leaf_names) is not list or len(leaf_names) != metadata.num_columns
                or any(type(name) is not str for name in leaf_names)
                or type(names) is not list or not 1 <= len(names) <= 64
                or any(type(name) is not str for name in names)
            ):
                raise MetadataError("invalid runner schema names")
            present = []
            nullable = set()
            declared = 0
            for name in FIELDS:
                leaf_count = leaf_names.count(name)
                field_count = names.count(name)
                if name in OPTIONAL_FIELDS and leaf_count == field_count == 0:
                    nullable.add(name)
                    continue
                if leaf_count != 1 or field_count != 1:
                    raise MetadataError("invalid runner field=" + name)
                field = schema.field(name)
                string_type = pa.types.is_string(field.type)
                null_type = name in OPTIONAL_FIELDS and pa.types.is_null(field.type)
                if not string_type and not null_type:
                    raise MetadataError("invalid runner field=" + name)
                if name in OPTIONAL_FIELDS and field.nullable:
                    nullable.add(name)
                if null_type and name not in nullable:
                    raise MetadataError("invalid runner field=" + name)
                present.append(name)
                column_index = leaf_names.index(name)
                for group_index in range(metadata.num_row_groups):
                    column = metadata.row_group(group_index).column(column_index)
                    size = column.total_uncompressed_size
                    if column.path_in_schema != name or not _bounded_integer(size, 0, MAX_PROJECTED_BYTES):
                        raise MetadataError("invalid runner projected bytes")
                    declared += size
                    if declared > MAX_PROJECTED_BYTES:
                        raise MetadataError("runner projected bytes exceeds 2097152")
            table = parquet.read(columns=present, use_threads=False, use_pandas_metadata=False)
            if table.num_rows != metadata.num_rows or table.column_names != present:
                raise MetadataError("invalid runner projected schema")
            records = table.to_pylist()
            if type(records) is not list or len(records) != metadata.num_rows:
                raise MetadataError("invalid runner projected rows")
        return _project(records, frozen, nullable)
    except MetadataError:
        raise
    except Exception:
        raise MetadataError("runner parquet read failed") from None
