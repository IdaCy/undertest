"""Conservative syntax eligibility for saved prior Bash commands.

This module never executes commands, expands words, or inspects files. An allowed
syntax tree is NOT a startup-state proof. Callers still need independently
accepted executable, environment, path/type, mount and complete-history evidence.
The original UTF-8 command and each token's spelling are retained unchanged.
"""

import hashlib
import re
from pathlib import PurePosixPath


class _Refusal(ValueError):
    pass


def _need(condition, reason):
    if not condition:
        raise _Refusal(reason)


def _lex(text):
    # Operators are recognized only outside quotes and escapes. Never feed
    # dequoted words back through an operator or expansion recognizer.
    tokens, i = [], 0
    while i < len(text):
        if text[i] in ' \t':
            i += 1
            continue
        start = i
        if text[i] in ';|&>':
            op = text[i]
            if text[i:i + 2] in ('&&', '||', '>&', '>>'):
                op = text[i:i + 2]
            _need(op in (';', '|', '&&', '||', '>', '>&'), 'unsupported_operator')
            i += len(op)
            tokens.append(dict(kind='operator', value=op, raw=op, start=start, end=i))
            continue
        value, quote = [], None
        while i < len(text):
            ch = text[i]
            if quote == "'":
                if ch == "'":
                    quote = None
                else:
                    value.append(ch)
                i += 1
                continue
            if quote == '"':
                if ch == '"':
                    quote = None
                    i += 1
                elif ch == '\\':
                    _need(i + 1 < len(text), 'unfinished_escape')
                    nxt = text[i + 1]
                    if nxt in '$`"\\':
                        value.append(nxt)
                    else:
                        value.extend(('\\', nxt))
                    i += 2
                else:
                    _need(ch not in '$`', 'expansion_not_supported')
                    value.append(ch)
                    i += 1
                continue
            if ch in ' \t;|&>':
                break
            _need(ch not in '$`*?[]~', 'expansion_not_supported')
            _need(ch not in '<(){}!#', 'unsupported_syntax')
            if ch in "'\"":
                quote = ch
                i += 1
            elif ch == '\\':
                _need(i + 1 < len(text), 'unfinished_escape')
                value.append(text[i + 1])
                i += 2
            else:
                value.append(ch)
                i += 1
        _need(quote is None, 'unfinished_quote')
        tokens.append(dict(kind='word', value=''.join(value), raw=text[start:i], start=start, end=i))
        _need(len(tokens) <= 512, 'token_bound')
    return tokens


def _number(value, *, zero=False):
    _need(re.fullmatch(r'[0-9]+', value) is not None and len(value) <= 7
          and (0 if zero else 1) <= int(value) <= 1000000, 'numeric_option_invalid')


def _path(value, root):
    _need(value != '' and '..' not in PurePosixPath(value).parts, 'path_not_project_literal')
    if value.startswith('/'):
        _need(value == root or value.startswith(root + '/'), 'path_outside_project')


def _files(values, root):
    for value in values:
        _path(value, root)


def _validate(words, root):
    name = words[0]['value']
    _need(words[0]['raw'] == name and name in ('cat', 'echo', 'find', 'grep', 'head', 'ls'),
          'command_not_allowed')
    args = [word['value'] for word in words[1:]]
    if name == 'echo':
        _need(all(not x.startswith('-') or x == '---' for x in args), 'echo_option_not_allowed')
        return
    if name == 'find':
        _need(bool(args), 'find_root_required')
        _path(args[0], root)
        _need(not args[0].startswith('-'), 'find_root_invalid')
        i, atom = 1, False
        while i < len(args):
            op = args[i]
            if op == '-o':
                _need(atom and i + 1 < len(args), 'find_expression_invalid')
                atom = False
                i += 1
                continue
            _need(op in ('-type', '-name', '-path', '-maxdepth') and i + 1 < len(args),
                  'find_predicate_not_allowed')
            value = args[i + 1]
            if op == '-type':
                _need(value in ('f', 'd'), 'find_type_invalid')
            elif op == '-maxdepth':
                _number(value, zero=True)
            else:
                _need(bool(value), 'find_pattern_empty')
            atom, i = True, i + 2
        return
    operands, options, pattern, i = [], True, None, 0
    while i < len(args):
        value = args[i]
        if options and value == '--':
            options = False
            i += 1
            continue
        if options and value.startswith('-') and value != '-':
            if name == 'ls':
                _need(re.fullmatch(r'-[la]+', value) is not None, 'ls_option_not_allowed')
            elif name == 'grep':
                _need(re.fullmatch(r'-[rilnvE]+', value) is not None
                      or (value.startswith('--include=') and len(value) > len('--include=')),
                      'grep_option_not_allowed')
            elif name == 'head':
                if value == '-n':
                    i += 1
                    _need(i < len(args), 'head_count_required')
                    _number(args[i])
                else:
                    _need(re.fullmatch(r'-[0-9]+', value) is not None, 'head_option_not_allowed')
                    _number(value[1:])
            else:
                raise _Refusal('cat_option_not_allowed')
        elif name == 'grep' and pattern is None:
            pattern = value
        elif value == '-' and name in ('cat', 'grep', 'head'):
            pass  # Literal stdin; its provenance is a separate proof premise.
        else:
            operands.append(value)
        i += 1
    _need(name != 'grep' or pattern is not None, 'grep_pattern_required')
    _files(operands, root)


class _Parser:
    def __init__(self, tokens, root):
        self.tokens, self.root, self.i = tokens, root, 0

    def operator(self, values):
        return self.i < len(self.tokens) and self.tokens[self.i]['kind'] == 'operator' and self.tokens[self.i]['value'] in values

    def simple(self):
        words, redirects = [], []
        while self.i < len(self.tokens) and not self.operator((';', '|', '&&', '||')):
            token = self.tokens[self.i]
            fd = 1
            if (token['kind'] == 'word' and token['raw'] in ('1', '2')
                    and self.i + 1 < len(self.tokens)
                    and self.tokens[self.i + 1]['kind'] == 'operator'
                    and self.tokens[self.i + 1]['value'] in ('>', '>&')
                    and token['end'] == self.tokens[self.i + 1]['start']):
                fd = int(token['raw'])
                self.i += 1
            if self.operator(('>', '>&')):
                _need(bool(words), 'leading_redirection_not_supported')
                op = self.tokens[self.i]
                self.i += 1
                _need(self.i < len(self.tokens) and self.tokens[self.i]['kind'] == 'word',
                      'redirection_target_required')
                target = self.tokens[self.i]
                if op['value'] == '>':
                    _need(target['value'] == target['raw'] == '/dev/null', 'file_redirection_not_allowed')
                else:
                    _need(target['value'] == target['raw'] and target['value'] in ('1', '2'),
                          'descriptor_redirection_not_allowed')
                redirects.append(dict(fd=fd, operator=op, target=target))
            else:
                _need(token['kind'] == 'word', 'simple_command_required')
                # Other adjacent IO numbers (0, 3, 12, ...) must not be treated
                # as harmless argv followed by an ordinary stdout redirection.
                if (token['raw'].isdigit() and self.i + 1 < len(self.tokens)
                        and self.tokens[self.i + 1]['kind'] == 'operator'
                        and self.tokens[self.i + 1]['value'] in ('>', '>&')
                        and token['end'] == self.tokens[self.i + 1]['start']):
                    raise _Refusal('descriptor_not_allowed')
                words.append(token)
            self.i += 1
        _need(bool(words), 'empty_command')
        _validate(words, self.root)
        return dict(words=words, redirects=redirects)

    def pipeline(self):
        commands = [self.simple()]
        while self.operator(('|',)):
            self.i += 1
            commands.append(self.simple())
        return dict(commands=commands)

    def and_or(self):
        first, rest = self.pipeline(), []
        while self.operator(('&&', '||')):
            op = self.tokens[self.i]
            self.i += 1
            rest.append(dict(operator=op, pipeline=self.pipeline()))
        return dict(first=first, rest=rest)

    def parse(self):
        clauses = [self.and_or()]
        while self.operator((';',)):
            self.i += 1
            if self.i == len(self.tokens):
                break
            clauses.append(self.and_or())
        _need(self.i == len(self.tokens), 'unparsed_suffix')
        return clauses


def classify_command(command, *, project_root='/workspace/project'):
    """Classify exact saved syntax, retaining original text and token spellings.

    No allowed result establishes read-only behavior of arbitrary executables,
    regular-file/symlink resolution, unchanged startup state, or actual custody.
    The grammar deliberately refuses newlines and all unquoted pathname globs.
    """
    if not isinstance(command, str):
        raise ValueError('command_must_be_text')
    raw = command.encode('utf-8')
    if (not isinstance(project_root, str) or not project_root.startswith('/')
            or str(PurePosixPath(project_root)) != project_root
            or '..' in PurePosixPath(project_root).parts or project_root == '/'):
        raise ValueError('project_root_invalid')
    result = dict(schema='causes.startup_command_syntax.v1', original_command=command,
                  command_sha256=hashlib.sha256(raw).hexdigest(), project_root=project_root,
                  syntactically_allowed=False, reason=None, syntax=None,
                  execution_performed=False, startup_state_established=False,
                  required_evidence=['exact saved invocation and prior-history custody',
                                     'reviewed executable/environment/loader identity',
                                     'project cwd and regular-file/symlink/pipe provenance',
                                     'unchanged startup mounts and HOME file absence'])
    try:
        _need(0 < len(raw) <= 16384, 'command_size_bound')
        _need(all(ord(c) >= 32 or c == '\t' for c in command)
              and '\x7f' not in command, 'control_character_or_newline')
        result['syntax'] = _Parser(_lex(command), project_root).parse()
        result['syntactically_allowed'] = True
    except _Refusal as error:
        result['reason'] = str(error)
    return result
