import unittest

from causes.fenced_spans import fenced_spans


class FencedSpansTests(unittest.TestCase):
    def test_inline_diagnostic_does_not_consume_later_opener(self):
        source = '```diagnostic```\r\nprose\n``` JS\r\nconst item = 1;\r\n```\r\nmore\n'
        spans = fenced_spans(source)
        self.assertEqual(len(spans), 1)
        self.assertEqual(source[spans[0]['start']:spans[0]['end']],
                         '``` JS\r\nconst item = 1;\r\n```\r\n')

    def test_shorter_and_nonblank_delimiters_do_not_close(self):
        source = '````py\n```\n``` blah\nvalue\n`````\ntext'
        span, = fenced_spans(source)
        self.assertTrue(span['closed'])
        self.assertEqual(source[span['end']:], 'text')

    def test_tilde_code_and_unicode_offsets(self):
        source = '☃\n~~~python\nprint(1)\n~~~\n'
        span, = fenced_spans(source)
        self.assertEqual(span['start'], 2)
        self.assertEqual(span['end'], len(source))

    def test_unclosed_block_is_conservatively_protected(self):
        source = 'intro\n``` js\ncode\n'
        span, = fenced_spans(source)
        self.assertFalse(span['closed'])
        self.assertEqual(source[span['start']:span['end']], '``` js\ncode\n')


if __name__ == '__main__':
    unittest.main()
