"""Prospective terminal-line extraction; scalar and completion rules are unchanged.

A separate bare boxed answer line may follow one complete math-envelope line.
Only that adjacent line is checked; earlier currency/prose is not its wrapper.
A bare terminal box may also follow a closed strong double-dollar display,
including a multiline display or same-line separator after its closing pair.
Earlier single-dollar currency is not a double-dollar wrapper.
The preserved balanced-envelope fallback remains conservative outside these forms.
"""
import re
from emergence.training.math_reward import _extract_answer, _Extraction, _BOX_RE, _FINAL_RE, _brace_group

_TOKEN = re.compile(r'(?<!\\)(?:\\\[|\\\]|\\\(|\\\)|\$\$|\$)')
_CLOSE = {r'\[': r'\]', r'\(': r'\)', '$': '$', '$$': '$$'}

_STRONG_TOKEN = re.compile(r'(?<!\\)(?:\\\[|\\\]|\\\(|\\\)|\$+)')


def _closed_double_display(prefix):
    """Only exact $$ and explicit backslash wrappers are structural outside math.

    Single-dollar tokens outside those wrappers can be currency and are ignored.
    No nesting, interleaving or long dollar run is admitted inside this grammar.
    """
    opened = None
    last = None
    for token in _STRONG_TOKEN.finditer(prefix):
        value = token.group()
        if opened is None:
            if value == '$':
                continue
            if value not in ('$$', r'\[', r'\('):
                return False
            opened = value
        elif value == _CLOSE[opened]:
            last = (opened, token.end())
            opened = None
        else:
            return False
    return (opened is None and last is not None and last[0] == '$$'
            and not prefix[last[1]:].strip())


def _preserved_extract_answer(final):
    original = _extract_answer(final)
    if original.reason not in ('box_trailing_content', 'unmatched_math_wrapper'):
        return original
    # A standalone answer line is not enclosed by the previous line's math.
    # This narrow form does not interpret dollar currency elsewhere in final.
    match = _BOX_RE.search(final)
    if match is not None and final.count(r'\boxed') == 1:
        answer, end, error = _brace_group(final, match.end() - 1)
        line_start = final.rfind('\n', 0, match.start()) + 1
        line_prefix = final[line_start:match.start()]
        preceding = final[:line_start].rstrip()
        # Strong $$ pairing is independent of earlier single-$ currency.
        # A failed pair must not fall through to the old single-line shortcut.
        if (not error and original.reason == 'unmatched_math_wrapper'
                and final[:match.start()].rstrip().endswith('$$')):
            if (not _closed_double_display(final[:match.start()])
                    or final[end:].strip() not in ('', '.')):
                return original
            markers = list(_FINAL_RE.finditer(final))
            if markers and (markers[0].end() > match.start()
                    or final[markers[0].end():match.start()].strip()):
                return _Extraction('invalid', 'conflicting_answer_markers')
            return _Extraction('valid', 'terminal_box_after_closed_double_display', answer)
        if (not error and original.reason == 'unmatched_math_wrapper'
                and not line_prefix.strip(' \t\r')
                and '\n' not in final[match.start():end]
                and '\r' not in final[match.start():end]
                and final[end:].strip() in ('', '.') and preceding):
            previous_line = preceding.split('\n')[-1].strip()
            tokens = list(_TOKEN.finditer(previous_line))
            # A bare/adjacent unclosed opener cannot become a standalone box.
            # Require exactly one complete, nonnested preceding envelope line.
            if (len(tokens) == 2 and tokens[0].start() == 0
                    and tokens[1].end() == len(previous_line)
                    and _CLOSE.get(tokens[0].group()) == tokens[1].group()):
                markers = list(_FINAL_RE.finditer(final))
                if markers and (markers[0].end() > match.start()
                        or final[markers[0].end():match.start()].strip()):
                    return _Extraction('invalid', 'conflicting_answer_markers')
                return _Extraction('valid', 'terminal_standalone_box_after_math_line', answer)

    # The original extractor already rejects multiple boxes and final markers.
    # Require every encountered math delimiter to form a nonnested matching pair.
    # This conservative scan can reject currency-like dollar prose; it never
    # guesses which unmatched dollar was meant to be mathematics.
    opened = None
    spans = []
    for token in _TOKEN.finditer(final):
        value = token.group()
        if opened is None:
            if value not in _CLOSE:
                return original
            opened = (value, token.start(), token.end())
        elif value == _CLOSE[opened[0]]:
            spans.append((opened[1], opened[2], token.start(), token.end()))
            opened = None
        else:
            return original
    if opened is not None or not spans:
        return original
    match = _BOX_RE.search(final)
    if match is None or final.count(r'\boxed') != 1:
        return original
    answer, end, error = _brace_group(final, match.end() - 1)
    if error:
        return original
    begin, inside, close, after = spans[-1]
    # A balanced preceding display is already closed. A terminal bare box
    # after it must not inherit its closing delimiter as an opening wrapper.
    if (original.reason == 'unmatched_math_wrapper'
            and after <= match.start()
            and not final[after:match.start()].strip()
            and final[end:].strip() in ('', '.')):
        markers = list(_FINAL_RE.finditer(final))
        if markers and (markers[0].end() > match.start()
                        or final[markers[0].end():match.start()].strip()):
            return _Extraction('invalid', 'conflicting_answer_markers')
        return _Extraction('valid', 'terminal_box_after_closed_math', answer)
    if not (inside <= match.start() < end <= close):
        return original
    if final[end:close].strip() or final[after:].strip() not in ('', '.'):
        return original
    markers = list(_FINAL_RE.finditer(final))
    if markers and (markers[0].end() > begin or final[markers[0].end():begin].strip()):
        return _Extraction('invalid', 'conflicting_answer_markers')
    return _Extraction('valid', 'terminal_box_balanced_math_envelope', answer)



def _terminal_block_start(final, box_start):
    """A blank line delimits an independent terminal answer paragraph.

    This is an explicit answer-format boundary, not an attempt to repair or
    interpret earlier malformed LaTeX. An opener immediately before the box,
    separated only by whitespace, still belongs to its terminal enclosure.
    """
    prefix = final[:box_start]
    boundaries = list(re.finditer(r'\r?\n[ \t]*\r?\n', prefix))
    start = boundaries[-1].end() if boundaries else 0
    tokens = list(_STRONG_TOKEN.finditer(prefix))
    if tokens and tokens[-1].start() < start and not prefix[tokens[-1].end():].strip():
        start = next((m.end() for m in reversed(boundaries)
                      if m.end() <= tokens[-1].start()), 0)
    return start


def _strong_display_spans(final, start):
    """Validate only the explicit terminal paragraph's strong enclosures."""
    opened = None
    spans = []
    for token in _STRONG_TOKEN.finditer(final, start):
        value = token.group()
        if opened is None:
            if value == '$':
                continue
            if value not in ('$$', r'\[', r'\('):
                return None
            opened = (value, token.start(), token.end())
        elif value == _CLOSE[opened[0]]:
            spans.append((opened[0], opened[1], opened[2], token.start(), token.end()))
            opened = None
        else:
            return None
    return None if opened is not None else spans


def _terminal_balanced_display_span(final, box_start, box_end):
    """Locate a complete final strong display independently of prose dollars."""
    spans = _strong_display_spans(final, _terminal_block_start(final, box_start))
    if not spans:
        return None
    _, begin, inside, close, after = spans[-1]
    if not (inside <= box_start < box_end <= close):
        return None
    if final[box_end:close].strip() or final[after:].strip() not in ('', '.'):
        return None
    return begin


def extract_answer(final):
    """Preserve scalar rules; validate only the terminal answer paragraph."""
    original = _preserved_extract_answer(final)
    if original.status == 'valid' and final.count(r'\boxed') == 1:
        match = _BOX_RE.search(final)
        if _strong_display_spans(final, _terminal_block_start(final, match.start())) is None:
            return _Extraction('invalid', 'unmatched_math_wrapper')
    if original.reason not in ('box_trailing_content', 'unmatched_math_wrapper'):
        return original
    match = _BOX_RE.search(final)
    if match is None or final.count(r'\boxed') != 1:
        return original
    answer, end, error = _brace_group(final, match.end() - 1)
    if error:
        return original
    start = _terminal_block_start(final, match.start())
    spans = _strong_display_spans(final, start)
    if spans is None:
        return original
    begin = _terminal_balanced_display_span(final, match.start(), end)
    reason = 'terminal_box_in_independent_math_display'
    if begin is None:
        # A terminal bare box must not inherit an earlier paragraph's markup.
        # Every strong span in its own region must end before the box, and
        # no non-whitespace/non-period content may follow the scalar box.
        if ((spans and spans[-1][4] > match.start())
                or final[end:].strip() not in ('', '.')):
            return original
        tokens = list(_STRONG_TOKEN.finditer(final, start, match.start()))
        if (tokens and tokens[-1].group() == '$'
                and not final[tokens[-1].end():match.start()].strip()
                and _preserved_extract_answer(final[start:]).status != 'valid'):
            # A directly adjacent lone-$ opener is an enclosure, not prose.
            # Keep completed single-dollar wrappers supported by the legacy
            # extractor on exactly this independently delimited region.
            return original
        begin = match.start()
        reason = 'terminal_box_in_independent_answer_paragraph'
    markers = list(_FINAL_RE.finditer(final))
    if markers and (markers[0].end() > begin or final[markers[0].end():begin].strip()):
        return _Extraction('invalid', 'conflicting_answer_markers')
    return _Extraction('valid', reason, answer)
