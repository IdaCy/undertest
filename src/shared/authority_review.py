"""Read an optional exact local review selected by an authenticated authority.

The caller authenticates the authority, its action and its validity interval.
This module grants no authority and never interprets process or service roles.
An absent selector leaves the caller's existing default path unchanged.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


MAX_REVIEW_BYTES = 16 * 1024**2


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _absolute(value, root):
    _require(type(value) is str and value and "\0" not in value, "review_path")
    path = Path(value)
    _require(str(path) == value and ".." not in path.parts, "review_path")
    return path if path.is_absolute() else root / path


def _pin(value, maximum):
    _require(type(value) is dict and set(value) == {"path", "bytes", "sha256"},
             "review_pin_fields")
    _require(type(value["bytes"]) is int and 0 <= value["bytes"] <= maximum,
             "review_byte_limit")
    _require(type(value["sha256"]) is str and
             re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is not None,
             "review_sha256")


def _directory(path):
    """Open every component without following symlinks, including ancestors."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _read(path, reference):
    parent = _directory(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_size == reference["bytes"],
                 "review_regular_exact_size")
        raw = bytearray()
        while len(raw) <= reference["bytes"]:
            block = os.read(descriptor, min(65536, reference["bytes"] + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        current_parent = _directory(path.parent)
        try:
            a, b = os.fstat(parent), os.fstat(current_parent)
            _require((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino),
                     "review_parent_changed")
        finally:
            os.close(current_parent)
        _require(_identity(before) == _identity(after) == _identity(named),
                 "review_changed_during_read")
        _require(len(raw) == reference["bytes"] and
                 hashlib.sha256(raw).hexdigest() == reference["sha256"],
                 "review_bytes_changed")
        return bytes(raw)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "review_duplicate_key")
        result[key] = value
    return result


def _number(value):
    number = float(value)
    _require(math.isfinite(number), "review_nonfinite_number")
    return number


def _constant(value):
    raise ValueError("review_nonfinite_constant")


def read_review_pin(reference, *, root, reviews_directory,
                    max_bytes=MAX_REVIEW_BYTES):
    """Read one exact review pin, without claiming authority or approval."""
    _require(type(max_bytes) is int and 0 < max_bytes <= MAX_REVIEW_BYTES,
             "review_limit")
    root = Path(root)
    _require(root.is_absolute() and ".." not in root.parts, "review_root")
    reviews = _absolute(str(reviews_directory), root)
    _require(reviews.is_relative_to(root), "review_directory_outside_root")
    _pin(reference, max_bytes)
    path = _absolute(reference["path"], root)
    _require(path != reviews and path.is_relative_to(reviews), "foreign_review")
    value = json.loads(_read(path, reference), object_pairs_hook=_object,
                       parse_float=_number, parse_constant=_constant)
    _require(type(value) is dict, "review_object_required")
    return path, value, reference["sha256"]


def read_selected_review(authority, *, root, reviews_directory,
                         selector="denied_process_review", max_bytes=MAX_REVIEW_BYTES):
    """Return ``(absolute_path, JSON_object, sha256)`` or None for the old default.

    A present selector must be an exact three-field pin, appear exactly once in
    the authority's evidence, and name a regular nonsymlink below this cohort's
    reviews directory. The bytes are rechecked on every call. Other evidence is
    validated structurally here; its authentication remains the caller's job.
    No selected-review failure falls back to the default.
    """
    _require(type(authority) is dict and type(selector) is str and selector,
             "review_authority_shape")
    if selector not in authority:
        return None
    _require(authority.get("root_authorized") is True, "review_authority_required")
    _require(type(max_bytes) is int and 0 < max_bytes <= MAX_REVIEW_BYTES,
             "review_limit")
    root = Path(root)
    _require(root.is_absolute() and ".." not in root.parts, "review_root")
    reviews = _absolute(str(reviews_directory), root)
    _require(reviews.is_relative_to(root), "review_directory_outside_root")
    reference = authority[selector]
    _pin(reference, max_bytes)
    path = _absolute(reference["path"], root)
    _require(path != reviews and path.is_relative_to(reviews), "foreign_review")
    evidence = authority.get("evidence")
    _require(type(evidence) is list and len(evidence) <= 4096, "review_evidence")
    names = set()
    matches = 0
    for item in evidence:
        _pin(item, 2**63 - 1)
        name = _absolute(item["path"], root)
        _require(name not in names, "duplicate_review_evidence")
        names.add(name)
        if name == path:
            _require(item == reference, "conflicting_review_evidence")
            matches += 1
    _require(matches == 1, "selected_review_not_in_evidence")
    return read_review_pin(reference, root=root, reviews_directory=reviews,
                           max_bytes=max_bytes)
