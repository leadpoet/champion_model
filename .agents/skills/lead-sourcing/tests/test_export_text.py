"""Workbook evidence excerpts remain readable without changing source meaning."""
import unittest

from export_text import source_excerpt


class SourceExcerptTests(unittest.TestCase):
    def test_markdown_page_headings_and_emphasis_become_readable_text(self):
        captured = ('# Construction\n\n'
                    '####\n\n'
                    '## **Professional Electrical Contracting**\n\n'
                    'Serving **industrial and institutional** projects.\n'
                    'Expired **September 9, 2026**.\n\n'
                    '[Project details](https://example.com/projects?a=1&b=2)')
        excerpt = source_excerpt(captured)
        self.assertNotIn('#', excerpt)
        self.assertNotIn('**', excerpt)
        self.assertIn('Project details (https://example.com/projects?a=1&b=2)', excerpt)
        self.assertIn('Professional Electrical Contracting', excerpt)
        self.assertIn('Expired September 9, 2026.', excerpt)
        self.assertIn('https://example.com/projects?a=1&b=2', excerpt)
        self.assertIn('## **Professional Electrical Contracting**', captured)

    def test_plain_technical_text_and_url_fragments_are_preserved(self):
        text = ('C# and C++ support; revenue < 5; headcount > 50.\n'
                '2 ** 3 = 8; package_name remains unchanged.\n'
                'Source: https://example.com/a_b#technical_section\n'
                'Pattern URL: https://example.com/**literal**')
        self.assertEqual(source_excerpt(text), text)

    def test_literal_code_keeps_its_markers(self):
        text = 'Example:\n```python\n# explanation\nvalue = "**literal**"\n```'
        self.assertEqual(source_excerpt(text), text)

    def test_long_markdown_excerpt_is_bounded_and_disclosed(self):
        text = '# A project\n\n' + '**Supported detail.** ' * 300
        excerpt = source_excerpt(text)
        self.assertLessEqual(len(excerpt), 2000)
        self.assertTrue(excerpt.endswith('[Excerpt; full text in saved receipt.]'))
        self.assertNotIn('**', excerpt)


if __name__ == '__main__':
    unittest.main()
