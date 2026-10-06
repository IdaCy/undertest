"""One representation-only fallback; no text rewriting or numeric evaluation."""
import re
from emergence.training.math_reward import _extract_answer, _Extraction, _BOX_RE, _FINAL_RE, _brace_group

_TOKEN = re.compile(r'(?<!\\)(?:\\\[|\\\]|\\\(|\\\)|\$\$|\$)')
_CLOSE = {r'\[': r'\]', r'\(': r'\)', '$': '$', '$$': '$$'}


def extract_answer(final):
    original = _extract_answer(final)
    if original.reason not in ('box_trailing_content', 'unmatched_math_wrapper'):
        return original
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
    if not (inside <= match.start() < end <= close):
        return original
    if final[end:close].strip() or final[after:].strip() not in ('', '.'):
        return original
    markers = list(_FINAL_RE.finditer(final))
    if markers and (markers[0].end() > begin or final[markers[0].end():begin].strip()):
        return _Extraction('invalid', 'conflicting_answer_markers')
    return _Extraction('valid', 'terminal_box_balanced_math_envelope', answer)
