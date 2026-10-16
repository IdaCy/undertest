"""Optional, bounded timing data; no work dispatch, persistence or admission policy.

One coordinator owns a Sampler. Its first sixteen begin() calls select samples
before outcomes are known. A sample retains at most forty integer-coded spans
and its compact JSON annotation is at most 4096 bytes. Callers keep the fixed
codebook and sampling intent; a missing annotation is not a successful sample.

Ordinary diagnostic Exceptions are contained. Control signals propagate unless
the measured body is already unwinding; diagnostics must not replace that body's
exception. Cancellation may occur before or after work. Exactly-once behavior
for ordinary diagnostic faults does not promise completion despite cancellation.
No exception text, arguments, paths, identifiers or payloads are recorded.
"""
import json
import time


MAX_SAMPLES = 16
MAX_SPANS = 40
MAX_SAMPLE_BYTES = 4096
_MAX_TICK = 2**63 - 1
_REASONS = frozenset(('clock_error', 'invalid_clock', 'invalid_stage',
                      'span_limit', 'clock_regressed', 'diagnostic_error',
                      'diagnostic_cancelled', 'encoding_error', 'byte_limit'))


class _NoSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


_NO_SPAN = _NoSpan()


class Sampler:
    """Invocation-local, outcome-independent selection; disabled uses no clock."""
    def __init__(self, *, enabled=False, clock=None):
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        self.enabled = enabled
        self.clock = time.monotonic_ns if clock is None else clock
        if not callable(self.clock):
            raise ValueError('clock must be callable')
        self.selected = 0

    def begin(self):
        if not self.enabled or self.selected >= MAX_SAMPLES:
            return None
        self.selected += 1
        try:
            return _Sample(self.selected, self.clock)
        except Exception:
            # The fixed intent still accounts for this selected, missing sample.
            return None


class _Sample:
    def __init__(self, ordinal, clock):
        self.ordinal = ordinal
        self.clock = clock
        self.rows = []
        self.missing = []
        self.stopped = False

    def note(self, reason):
        try:
            if reason not in self.missing:
                self.missing.append(reason)
        except Exception:
            pass

    def tick(self):
        try:
            value = self.clock()
        except Exception:
            self.note('clock_error')
            self.stopped = True
            return None
        if type(value) is not int or not 0 <= value <= _MAX_TICK:
            self.note('invalid_clock')
            self.stopped = True
            return None
        return value

    def start(self, code):
        if self.stopped:
            return _NO_SPAN
        if type(code) is not int or not 0 <= code <= 255:
            self.note('invalid_stage')
            return _NO_SPAN
        if len(self.rows) >= MAX_SPANS:
            self.note('span_limit')
            self.stopped = True
            return _NO_SPAN
        row = {'code': code, 'elapsed_ns': None, 'raised': None}
        self.rows.append(row)
        return _Span(self, row)


class _Span:
    def __init__(self, sample, row):
        self.sample = sample
        self.row = row
        self.start_tick = None

    def __enter__(self):
        try:
            self.start_tick = self.sample.tick()
        except Exception:
            self.sample.note('diagnostic_error')
            self.sample.stopped = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.row['raised'] = exc_type is not None
            if self.start_tick is not None and not self.sample.stopped:
                end = self.sample.tick()
                if end is not None:
                    if end < self.start_tick:
                        self.sample.note('clock_regressed')
                        self.sample.stopped = True
                    else:
                        self.row['elapsed_ns'] = end - self.start_tick
        except BaseException as error:
            if not isinstance(error, Exception) and exc_type is None:
                raise
            try:
                self.sample.note('diagnostic_error' if isinstance(error, Exception)
                                 else 'diagnostic_cancelled')
                self.sample.stopped = True
            except BaseException:
                if exc_type is None:
                    raise
        return False


def span(sample, code):
    """Wrap a block, never its callable. None/exhausted means no clock calls."""
    if type(sample) is not _Sample:
        return _NO_SPAN
    try:
        return sample.start(code)
    except Exception:
        sample.note('diagnostic_error')
        sample.stopped = True
        return _NO_SPAN


def annotation(sample, *, unwinding=False):
    """Return a detached, size-checked object, or None on diagnostic failure.

    The caller may omit it if the existing record has insufficient room. This
    function does not write, serialize the caller's record, or retry any work.
    In an existing exception handler pass unwinding=True so even a diagnostic
    control signal cannot replace the original exception being propagated.
    """
    if type(sample) is not _Sample:
        return None
    try:
        if (type(sample.ordinal) is not int or not 1 <= sample.ordinal <= MAX_SAMPLES
                or len(sample.rows) > MAX_SPANS
                or len(sample.missing) > len(_REASONS)
                or any(type(x) is not str or x not in _REASONS for x in sample.missing)):
            raise ValueError('invalid timing data')
        rows = []
        for row in sample.rows:
            code, elapsed, raised = row['code'], row['elapsed_ns'], row['raised']
            if (type(code) is not int or not 0 <= code <= 255
                    or (elapsed is not None and (type(elapsed) is not int
                        or not 0 <= elapsed <= _MAX_TICK))
                    or (raised is not None and type(raised) is not bool)):
                raise ValueError('invalid timing data')
            rows.append({'code': code, 'elapsed_ns': elapsed, 'raised': raised})
        result = {'schema': 'shared.bounded_timing.v1',
                  'ordinal': sample.ordinal,
                  'spans': rows,
                  'missing': list(sample.missing)}
        data = json.dumps(result, separators=(',', ':'), allow_nan=False).encode()
        if len(data) <= MAX_SAMPLE_BYTES:
            return result
        sample.note('byte_limit')
    except BaseException as error:
        if not isinstance(error, Exception) and not unwinding:
            raise
        try:
            sample.note('encoding_error')
        except BaseException:
            if not unwinding:
                raise
    return None
