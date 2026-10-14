import copy
import hashlib
import unittest
from unittest.mock import patch

from causes.startup_command_grammar import classify_command


class StartupCommandGrammarTests(unittest.TestCase):
    def allowed(self, text):
        result = classify_command(text)
        self.assertTrue(result['syntactically_allowed'], (text, result['reason']))
        self.assertEqual(result['original_command'], text)
        self.assertEqual(result['command_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertFalse(result['startup_state_established'])
        self.assertFalse(result['execution_performed'])
        self.assertTrue(result['required_evidence'])
        return result

    def refused(self, text):
        result = classify_command(text)
        self.assertFalse(result['syntactically_allowed'], text)
        self.assertIsNone(result['syntax'])
        self.assertTrue(result['reason'])
        self.assertEqual(result['original_command'], text)
        self.assertFalse(result['startup_state_established'])

    def test_observed_list_and_pipeline(self):
        r = self.allowed('find /workspace/project -type f -name "*.md" | head -20 && echo "---" && ls /workspace/project')
        self.assertEqual(len(r['syntax'][0]['first']['commands']), 2)
        self.assertEqual([x['operator']['value'] for x in r['syntax'][0]['rest']], ['&&', '&&'])

    def test_list_precedence_and_all_branches(self):
        r = self.allowed('ls || cat README | head; grep -n text README && echo ---;')
        self.assertEqual(len(r['syntax']), 2)
        self.assertEqual(len(r['syntax'][0]['rest'][0]['pipeline']['commands']), 2)
        for text in ('ls || touch x', 'ls && touch x', 'ls | touch x', 'ls; touch x'):
            with self.subTest(text=text): self.refused(text)

    def test_quoted_operators_remain_word_data(self):
        for spelling in ("'a; touch x | && > z'", '"a; touch x | && > z"', r'a\;b\|c'):
            r = self.allowed('echo ' + spelling)
            command = r['syntax'][0]['first']['commands'][0]
            self.assertEqual(len(command['words']), 2)
            self.assertEqual(command['redirects'], [])
            self.assertEqual(command['words'][1]['raw'], spelling)

    def test_quote_concatenation_is_not_reparsed(self):
        r = self.allowed('grep --include="*.java" "a"\';\'"b" /workspace/project/file')
        words = r['syntax'][0]['first']['commands'][0]['words']
        self.assertEqual(words[1]['value'], '--include=*.java')
        self.assertEqual(words[2]['value'], 'a;b')

    def test_escape_semantics_preserve_double_quote_backslash(self):
        r = self.allowed(r'grep "\q" README')
        self.assertEqual(r['syntax'][0]['first']['commands'][0]['words'][1]['value'], r'\q')
        r = self.allowed(r'echo "\$HOME"')
        self.assertEqual(r['syntax'][0]['first']['commands'][0]['words'][1]['value'], '$HOME')

    def test_literal_dollar_is_data_only_with_proven_quotes_or_escape(self):
        for text in ("echo '$(touch /tmp/x)'", r'echo \$HOME', "grep '$HOME' README"):
            self.allowed(text)
        for text in ('echo $HOME', 'echo "$HOME"', 'echo ${HOME}', 'echo $(ls)', 'echo $((1+2))', 'echo `ls`', 'echo "`ls`"', "echo $'x'"):
            with self.subTest(text=text): self.refused(text)

    def test_all_newlines_and_controls_refuse(self):
        for text in ('ls\nls', "echo 'x\ny'", 'echo "x\ny"', 'ls\\\n;ls', 'ls\r;ls', 'ls\x00', 'ls\x7f', 'ls\v'):
            with self.subTest(text=repr(text)): self.refused(text)
        self.allowed('ls\t-la\t/workspace/project')

    def test_unquoted_globs_refuse_in_every_position(self):
        for text in ('* README', 'cat /workspace/project/README*', 'find /workspace/project -name *.c', 'find /workspace/project *', 'grep --include=*.java text README', 'ls x?', 'ls [ab]', 'ls ~'):
            with self.subTest(text=text): self.refused(text)
        self.allowed('find /workspace/project -name "*".c')
        self.allowed("cat '/workspace/project/README*'")
        self.allowed(r'cat /workspace/project/README\*')

    def test_find_only_explicit_predicates(self):
        self.allowed('find /workspace/project -maxdepth 2 -type f -name "*.java" -o -path "*thread*"')
        self.allowed('find /workspace/project')
        self.allowed('find /workspace/project -name -delete')
        for suffix in ('-delete', '-exec echo x \\;', '-execdir echo x \\;', '-ok echo x \\;', '-fprint /tmp/x', '-fls /tmp/x', '-print', '-printf x', '-type l', '-maxdepth -1', '-maxdepth 1000001', '-name', '-o -name x', '-name x -o', '-name x -o -o -name y', '\\( -name x \\)'):
            with self.subTest(suffix=suffix): self.refused('find /workspace/project ' + suffix)

    def test_paths_need_separate_evidence_and_cannot_escape_project(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem inspected')):
            self.allowed('cat not-known-to-exist README.md /workspace/project/src/a')
        for text in ('cat /etc/profile', 'ls /workspace/projected', 'head ../README', 'find / -type f', 'ls /workspace/project/../x'):
            with self.subTest(text=text): self.refused(text)

    def test_grep_patterns_options_and_files(self):
        self.allowed('grep -rn "connection" /workspace/project/internal --include="*.go" -l | grep -v _test | grep -iv test | head -40')
        self.allowed('grep -- -n README')
        self.allowed('grep -E "null|yes"')
        for text in ('grep -f patterns README', 'grep --exclude=x x README', 'grep -R x README', 'grep --include= x README', 'grep -n', 'grep -n x /proc/self/mem'):
            with self.subTest(text=text): self.refused(text)

    def test_ls_cat_echo_and_head_option_allowlists(self):
        for text in ('ls -la', 'ls -al /workspace/project', 'cat -- -n', 'cat -', 'head -20 README', 'head -n 4 README', 'head', 'echo ---'):
            self.allowed(text)
        for text in ('ls --format=long', 'cat -n README', 'echo -e x', 'echo -n x', 'head -c 4 README', 'head -n', 'head -n 0 README', 'head -0 README', 'head -n -1 README'):
            with self.subTest(text=text): self.refused(text)

    def test_exact_null_and_descriptor_redirections(self):
        for text in ('ls 2>/dev/null', 'ls >/dev/null', 'ls 1>/dev/null', 'ls 2>&1', 'ls 1>&2', 'ls >&2', 'ls 2>/dev/null | head'):
            self.allowed(text)
        for text in ('ls >file', 'ls >>/dev/null', 'ls </dev/null', 'ls 0>/dev/null', 'ls 3>/dev/null', 'ls 12>&1', 'ls 2>&0', 'ls 1>&-', 'ls 2>&file', 'ls &>/dev/null', 'ls >"/dev/null"', '>/dev/null ls'):
            with self.subTest(text=text): self.refused(text)

    def test_io_number_adjacency_and_quote_origin(self):
        r = self.allowed('echo 2 >/dev/null')
        c = r['syntax'][0]['first']['commands'][0]
        self.assertEqual(c['words'][1]['value'], '2')
        self.assertEqual(c['redirects'][0]['fd'], 1)
        r = self.allowed('echo "2">/dev/null')
        c = r['syntax'][0]['first']['commands'][0]
        self.assertEqual(c['words'][1]['raw'], '"2"')
        self.assertEqual(c['redirects'][0]['fd'], 1)
        r = self.allowed('echo 2>/dev/null')
        self.assertEqual(r['syntax'][0]['first']['commands'][0]['redirects'][0]['fd'], 2)

    def test_reject_scope_expanders_and_shell_constructs(self):
        for text in ('cd /workspace/project', 'sed -n 1,2p README', 'git status', 'java -version', 'javac -version', 'which cat', 'env ls', 'command ls', 'builtin echo x', 'eval "ls"', '. file', 'source file', 'X=x ls', 'export X=x', '(ls)', '{ ls; }', 'ls &', 'ls |& head', 'for x in a; do ls; done', 'ls # hidden', 'trap x EXIT', 'cat <<EOF', 'echo <(ls)', 'echo >(ls)', 'l"s"', '! ls'):
            with self.subTest(text=text): self.refused(text)

    def test_malformed_quote_escape_and_composition(self):
        for text in ('', ' ', "echo 'x", 'echo "x', 'echo \\', ';ls', 'ls;;ls', 'ls ||', 'ls &&', 'ls |', 'ls >', 'ls && ; ls'):
            with self.subTest(text=text): self.refused(text)

    def test_mutations_do_not_hide_a_second_command(self):
        base = 'grep "text" README'
        for suffix in ('; touch x', ' && touch x', ' || touch x', ' | touch x', '\ntouch x', ' >README', ' $(touch x)', ' `touch x`'):
            with self.subTest(suffix=suffix): self.refused(base + suffix)
        self.allowed('grep "text; touch x" README')

    def test_token_spelling_and_original_are_immutable(self):
        text = 'grep "日本語;|"  README'
        r = self.allowed(text)
        for word in r['syntax'][0]['first']['commands'][0]['words']:
            self.assertEqual(word['raw'], text[word['start']:word['end']])
        frozen = copy.deepcopy(r)
        r['syntax'][0]['first']['commands'][0]['words'][1]['value'] = 'changed'
        self.assertEqual(classify_command(text), frozen)

    def test_bounds_and_root_validation(self):
        self.refused('echo ' + 'x' * 16384)
        self.refused('echo ' + 'x ' * 513)
        with self.assertRaises(ValueError): classify_command(None)
        for root in ('/', 'relative', '/a/../b', '/a//b', '/a/'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                classify_command('ls', project_root=root)


if __name__ == '__main__':
    unittest.main()
