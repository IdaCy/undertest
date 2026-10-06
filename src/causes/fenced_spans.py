import re


def fenced_spans(text):
    """Return exact source offsets for line-delimited Markdown code blocks."""
    spans = []
    opened = None
    offset = 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip('\r\n')
        if opened:
            closer = re.fullmatch(
                r'[ \t]*(' + re.escape(opened['character']) + '{' +
                str(opened['length']) + r',})[ \t]*', bare)
            if closer:
                spans.append({'start': opened['start'], 'end': offset + len(line),
                              'reason': 'line_state_fenced_code', 'closed': True})
                opened = None
        else:
            opener = re.fullmatch(r'[ \t]*(`{3,}|~{3,})([^\r\n]*)', bare)
            if opener and not (opener[1][0] == '`' and '`' in opener[2]):
                opened = {'start': offset, 'character': opener[1][0],
                          'length': len(opener[1])}
        offset += len(line)
    if opened:
        spans.append({'start': opened['start'], 'end': len(text),
                      'reason': 'line_state_fenced_code', 'closed': False})
    return spans
