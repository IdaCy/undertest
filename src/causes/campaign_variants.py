from __future__ import annotations

import hashlib
import json
import re


PROTECTED = re.compile(r'```[^\n]*\n[\s\S]*?```|~~~[^\n]*\n[\s\S]*?~~~|`[^`\n]+`|https?://[^\s<>]+|\b[\w/-]+\.[\w./-]+\b')
EXTRA_PROTECTED = [
    r'(?m)^(?: {4}|\t).+$',
    r'(?ms)^#{1,6}\s+Code\s*\r?\n.*?(?=^#{1,6}\s|\Z)',
    r'(?is)<([a-z][a-z0-9]*)\b[^>]*>.*?</\1\s*>',
    r'<[^>\n]+>',
    r'"[^"\n]*"|(?<![A-Za-z])\x27[^\x27\n]*\x27',
    r'(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*(?:=[^\s]+)?',
    r'(?<!\w)(?:[~.]?/|[A-Za-z0-9_-]+/)[A-Za-z0-9_./-]+',
    r'\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b',
    r'\b(?:[A-Z][a-z]+){2,}[A-Za-z0-9]*\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b',
    r'\b[A-Z][A-Z0-9_]{1,}\b',
    r'\b[A-Za-z_][A-Za-z0-9_]*(?=\()',
    r'\b(?:README|Makefile|GNUmakefile|LICENSE|Dockerfile|Gemfile|Rakefile)\b',
]
WORD = re.compile(r'(?<![\w/.-])[A-Za-z]{4,}(?![\w/.-])')


def prose_mask(text, protected_spans=()):
    mask = [True] * len(text)
    for pattern in [PROTECTED, *EXTRA_PROTECTED]:
        for match in re.finditer(pattern, text):
            mask[match.start():match.end()] = [False] * len(match.group())
    for span in protected_spans:
        start, end = span['start'], span['end']
        if not 0 <= start <= end <= len(text):
            raise ValueError('invalid_protected_span')
        mask[start:end] = [False] * (end - start)
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char not in '{[':
            continue
        try:
            value, length = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            mask[start:start+length] = [False] * length
    return mask


def ranked(indices, task_id, factor):
    return sorted(indices, key=lambda value: hashlib.sha256(f'{task_id}|{factor}|{value}'.encode()).digest())


def typo_positions(text, task_id, protected_spans=()):
    mask = prose_mask(text, protected_spans)
    protected_words = {word.lower() for match in PROTECTED.finditer(text) for word in re.findall('[A-Za-z]+', match.group())}
    candidates = []
    for match in WORD.finditer(text):
        word = match.group()
        if not all(mask[match.start():match.end()]) or word.lower() in protected_words:
            continue
        if not (word.islower() or word.istitle()):
            continue
        for offset in range(1, len(word) - 2):
            if word[offset] != word[offset+1]:
                candidates.append(match.start() + offset)
                break
    return ranked(candidates, task_id, 'typos')


def case_positions(text, task_id, protected_spans=()):
    mask = prose_mask(text, protected_spans)
    return ranked([index for index, char in enumerate(text) if mask[index] and char.isascii() and char.isupper()], task_id, 'casing')


def mechanical(text, task_id, protected_spans=()):
    mask = prose_mask(text, protected_spans)
    rows = []
    typos = typo_positions(text, task_id, protected_spans)
    caps = case_positions(text, task_id, protected_spans)
    for level, count in enumerate([1, 2, 4, 8, 16], 1):
        if len(typos) < count:
            rows.append({'factor': 'F01', 'level': level, 'eligible': False, 'reason': 'insufficient_unprotected_typo_positions', 'available': len(typos)})
            continue
        chars = list(text)
        for position in typos[:count]:
            chars[position], chars[position+1] = chars[position+1], chars[position]
        rows.append({'factor': 'F01', 'level': level, 'eligible': True, 'request': ''.join(chars), 'positions': typos[:count]})
    for level, count in enumerate([1, 2, 4, 8, len(caps)], 1):
        if len(caps) < count or not count:
            rows.append({'factor': 'F02', 'level': level, 'eligible': False, 'reason': 'insufficient_original_uppercase_characters', 'available': len(caps)})
            continue
        chars = list(text)
        for position in caps[:count]:
            chars[position] = chars[position].lower()
        rows.append({'factor': 'F02', 'level': level, 'eligible': True, 'request': ''.join(chars), 'positions': caps[:count]})
    periods = [i for i, char in enumerate(text) if char == '.' and mask[i]]
    commas = [i for i, char in enumerate(text) if char == ',' and mask[i]]
    for level in range(1, 6):
        removed = set(periods[-1:] if level == 1 else periods)
        if level >= 3:
            removed.update(commas)
        if not periods or (level >= 3 and not commas):
            rows.append({'factor':'F04', 'level':level, 'eligible':False, 'reason':'requested_punctuation_absent'})
            continue
        inserted = set()
        if level == 5:
            if len(typos) < 2:
                rows.append({'factor':'F04', 'level':level, 'eligible':False, 'reason':'insufficient_misplaced_comma_positions'})
                continue
            inserted = set(typos[:2])
        value = ''.join((',' if i in inserted else '') + char for i, char in enumerate(text) if i not in removed)
        symbol = '~!?%*'[int(hashlib.sha256(task_id.encode()).hexdigest()[:4],16)%5]
        if level >= 4:
            value += '\n' + symbol
        rows.append({'factor':'F04', 'level':level, 'eligible':True, 'request':value, 'removed_positions':sorted(removed)})
    return rows


def assert_protected_equal(before, after):
    blocks = [match.group() for match in PROTECTED.finditer(before)]
    # Newly misspelled prose can accidentally resemble a protected token. Only original spans are invariant.
    cursor = 0
    for block in blocks:
        position = after.find(block, cursor)
        if position < 0:
            raise ValueError('protected_source_span_changed')
        cursor = position + len(block)
