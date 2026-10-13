"""Bounded pipe collection; callers own intent and durable result recording.

A handled failure returns the exact collected prefix of each stream. Reaching
a byte cap is conservatively incomplete, even if the child would next close the
pipe. No extra byte is read and discarded to distinguish that case. Abrupt loss
of the parent leaves no qualified capture; this helper does not recover or retry.
"""

from dataclasses import dataclass
import math
import os
import selectors
import subprocess
import time


MAX_BYTES = 512 * 1024**2
CHUNK = 64 * 1024


@dataclass(frozen=True)
class Capture:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    pid: int | None
    complete: bool
    reason: str | None
    error_type: str | None
    stdin_written: int
    elapsed_seconds: float
    termination_requested: bool
    child_reaped: bool
    cleanup_errors: tuple[str, ...]


def _number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def collect(argv, *, input_bytes=b'', timeout, stdout_limit, stderr_limit,
            total_limit, input_limit=16 * 1024**2, cleanup_timeout=1.0,
            cwd=None, env=None):
    """Run exactly one direct child with bounded, concurrently serviced pipes.

    Limits bound retained bytes, not the child's output or OS-wide memory. Peak
    Python buffer allowance should include at least three times total_limit,
    input_limit and interpreter overhead. Cleanup has its own bounded deadline;
    it kills only this Popen child, never a process group or an arbitrary PID.
    A zero exit with complete=True is necessary for a usable transport receipt.
    """
    if (type(argv) not in (list, tuple) or not argv or
            any(type(arg) is not str or not arg or '\x00' in arg for arg in argv)):
        raise ValueError('invalid_argv')
    for value in (stdout_limit, stderr_limit, total_limit, input_limit):
        if type(value) is not int or not 0 < value <= MAX_BYTES:
            raise ValueError('invalid_byte_limit')
    if type(input_bytes) is not bytes or len(input_bytes) > input_limit:
        raise ValueError('invalid_input')
    if not _number(timeout, 3600) or not _number(cleanup_timeout, 5):
        raise ValueError('invalid_timeout')
    start = time.monotonic()
    deadline = start + timeout
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    limits = {'stdout': stdout_limit, 'stderr': stderr_limit}
    proc = None
    selector = None
    reason = error_type = None
    written = 0
    output_eof = set()
    termination_requested = False
    cleanup_errors = []
    reaped = False
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0, close_fds=True,
                                cwd=cwd, env=env)
        selector = selectors.DefaultSelector()
        for name in ('stdout', 'stderr'):
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input_bytes:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            proc.stdin.close()
        while reason is None:
            if output_eof == {'stdout', 'stderr'} and written == len(input_bytes):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'timeout'
                    break
                try:
                    proc.wait(timeout=remaining)
                    reaped = True
                except subprocess.TimeoutExpired:
                    reason = 'timeout'
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = 'timeout'
                break
            for key, unused in selector.select(remaining):
                name = key.data
                if name == 'stdin':
                    try:
                        count = os.write(key.fd, memoryview(input_bytes)[written:written + CHUNK])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        reason = 'stdin_closed'
                        break
                    written += count
                    if written == len(input_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                available = min(limits[name] - len(buffers[name]),
                                total_limit - sum(map(len, buffers.values())))
                if available <= 0:
                    reason = 'output_limit_reached'
                    break
                try:
                    chunk = os.read(key.fd, min(CHUNK, available))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    output_eof.add(name)
                else:
                    buffers[name].extend(chunk)
                    if (len(buffers[name]) == limits[name] or
                            sum(map(len, buffers.values())) == total_limit):
                        reason = 'output_limit_reached'
                        break
    except Exception as error:
        reason = 'spawn_error' if proc is None else 'capture_error'
        error_type = type(error).__name__
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    termination_requested = True
                    proc.kill()
                proc.wait(timeout=cleanup_timeout)
                reaped = True
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        if selector is not None:
            try:
                selector.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
    complete = (reason is None and reaped and not cleanup_errors and
                written == len(input_bytes) and output_eof == {'stdout', 'stderr'})
    if not complete and reason is None:
        reason = 'cleanup_error'
    return Capture(bytes(buffers['stdout']), bytes(buffers['stderr']),
                   None if proc is None else proc.returncode,
                   None if proc is None else proc.pid, complete, reason, error_type,
                   written, time.monotonic() - start, termination_requested, reaped,
                   tuple(cleanup_errors))
