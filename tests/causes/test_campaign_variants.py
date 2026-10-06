import unittest

from causes.campaign_variants import mechanical


class MechanicalSemanticPreservationTests(unittest.TestCase):
    def assert_literals_survive(self, text, literals):
        text += ' Please inspect the existing behavior carefully, preserve compatibility, and implement the requested changes. Review the related examples before testing the expected behavior.'
        for row in mechanical(text, 'independent-protection-case'):
            if not row.get('eligible'):
                continue
            with self.subTest(factor=row['factor'], level=row['level']):
                for literal in literals:
                    self.assertIn(literal, row['request'])

    def test_case_sensitive_cli_flags_and_environment_variables(self):
        self.assert_literals_survive('Honor NO_COLOR and preserve the behavior of -M and -C.', ['NO_COLOR', '-M', '-C'])

    def test_bare_paths_and_extensionless_files(self):
        self.assert_literals_survive('Update /Config/Runtime/Cache while preserving README and Makefile.',
                                    ['/Config/Runtime/Cache', 'README', 'Makefile'])

    def test_bare_function_and_type_identifiers(self):
        self.assert_literals_survive('Repair getUserName in ArrayStore and preserve TypeError.',
                                    ['getUserName', 'ArrayStore', 'TypeError'])

    def test_html_tag_and_attribute_values(self):
        self.assert_literals_survive('Preserve <button disabled onclick="handleClick()">Submit</button>.',
                                    ['<button disabled onclick="handleClick()">Submit</button>'])

    def test_json_keys_and_literals(self):
        self.assert_literals_survive('Return {"enabled":true,"timeout":100} as the configuration.',
                                    ['{"enabled":true,"timeout":100}'])

    def test_exact_output_strings_and_literal_punctuation(self):
        self.assert_literals_survive('The returned string must equal "Success" and delimiters must be \'.\' and \',\'.',
                                    ['"Success"', "'.'", "','"])


if __name__ == '__main__':
    unittest.main()
