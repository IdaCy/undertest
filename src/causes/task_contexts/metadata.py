"""Verify explicitly selected metadata bytes before lazy column projection."""

import hashlib
import os
import re
import stat

MAX_SOURCE_BYTES = 2 * 1024 * 1024

class MetadataError(ValueError):
    """A bounded error whose text contains no input values."""

def _pinned_bytes(path, selected_path, expected_size, expected_sha256):
    if type(path) is not str or type(selected_path) is not str or path != selected_path:
        raise MetadataError("invalid selected source path")
    if not 1 <= len(path) <= 4096:
        raise MetadataError("invalid selected source path")
    parts = path.split("/")
    if not path.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]):
        raise MetadataError("invalid selected source path")
    if type(expected_size) is not int or not 1 <= expected_size <= MAX_SOURCE_BYTES:
        raise MetadataError("invalid source size")
    if type(expected_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise MetadataError("invalid source digest")
    directory = None
    descriptor = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for component in parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise MetadataError("invalid source size or type")
        chunks = []
        remaining = expected_size + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise MetadataError("source pin mismatch")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise MetadataError("source changed during verification")
        return content
    except MetadataError:
        raise
    except Exception:
        raise MetadataError("source verification failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
