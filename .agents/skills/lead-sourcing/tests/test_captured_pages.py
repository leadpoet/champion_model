"""Replay the ContextDev list shape through the existing qualification journey."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_tools import FixtureProvider, check
from research_tools import ResearchTools
import budget_guard
import deepline
import scrapingdog
import source_receipts
import validate_run
from source_receipts import source_date


URL = 'https://example.test/news/acquisition'
TEXT = 'The PE-backed Ohio manufacturer completed an acquisition on August 26, 2026.'


def page():
    # Reduced captured ContextDev shape; content and identity are synthetic.
    return {'markdown': TEXT, 'metadata': {'sourceUrl': URL, 'finalUrl': URL,
        'url': URL, 'statusCode': 200, 'success': True, 'publishedTime': '2026-08-26T08:04:56+00:00'}}


def response(rows):
    return {'status': 'completed', 'toolResponse': {'rawV2': {'results': rows}}}


class CapturedPageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'results.json'
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.tools.start({'target_count': 1, 'as_of_date': '2026-09-17', 'contact_fields': [],
            'requested_roles': ['Operations leader'], 'icp': {'exclusions': ['excluded.test']},
            'buying_signals': [{'kind': 'FACILITY_OPENING', 'importance': 'required',
                'max_age_days': 365, 'query': 'Completed acquisition'}]}, max_usd=1)

    def capture(self, row=None, url=URL):
        self.provider.raw = response([page() if row is None else row])
        result = self.tools.lookup([check(tool='contextdev_post_web_crawl', inputs={'url': url},
                                         purpose='Read captured page ' + url)])
        return result['lookups'][0]['results'][0]['ref']

    def finding(self, ref, **evidence):
        return {'target': 'example.test', 'decision': 'qualify_account', 'reason': 'Captured acquisition',
            'account_fit': {'ref': ref},
            'qualification_checks': [{'requirement_ref': 'signal:0', 'status': 'pass',
                'claim': 'Completed acquisition', 'evidence': [{'ref': ref,
                    'event_date': '2026-08-26', **evidence}]}]}

    def test_news_description_cannot_qualify_until_the_page_is_captured(self):
        self.provider.raw = response([{'type': 'editorial', 'url': URL,
            'description': TEXT, 'date': '2026-08-26', 'content_kind': 'structured_record'}])
        lookup = self.tools.lookup([check(tool='contextdev_post_news_search', inputs={'query': 'Example acquisition'})])
        ref = lookup['lookups'][0]['results'][0]['ref']
        receipt = self.path.parent / 'receipts' / (ref.split(':')[0] + '.json')
        original = receipt.read_bytes()
        before = budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, 'search excerpts'):
            self.tools.review(companies=[self.finding(ref)])
        self.assertEqual((budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        # A previously saved approval cannot bypass the same export preflight.
        evidence = self.tools._evidence({'ref': ref, 'event_date': '2026-08-26'})
        doc = self.tools._document()
        doc['accepted'] = [{'company': {'domain': 'example.test'}, 'qualification_checks': [{
            'criterion': 'FACILITY_OPENING', 'signal': 'FACILITY_OPENING', 'importance': 'required',
            'status': 'pass', 'claim': TEXT, 'evidence': [evidence]}]}]
        self.assertIn('search excerpts', ' '.join(validate_run.qualification_errors(doc, run_file=self.path)))
        sources = {}
        self.tools._company_review(doc['accepted'][0], sources)
        self.assertEqual(sources[ref]['content_kind'], 'search_excerpt')
        captured = self.capture()
        self.tools.review(companies=[self.finding(captured)])
        self.assertEqual(self.tools._document()['unresolved'][0]['stage'], 'contact')
        self.assertEqual(receipt.read_bytes(), original)

    def test_page_label_on_unknown_provider_text_does_not_invent_a_capture(self):
        row = deepline.normalize_evidence({'signal': 'web_page', 'text': TEXT, 'url': URL,
            'content_kind': 'captured_page'}, tool='unfamiliar_search')
        self.assertEqual(row['content_kind'], 'search_excerpt')

    def test_generic_http_html_is_research_visible_but_cannot_qualify_or_redispatch(self):
        html = '<!DOCTYPE html><html><body>' + TEXT + '</body></html>'
        self.provider.rate = 0
        self.provider.raw = {'status': 'completed', 'job_id': 'saved-generic-job',
            'result': {'data': html}}
        lookup = self.tools.lookup([check(tool='generic_http_request',
            inputs={'url': URL}, purpose='Read generic public HTML')])
        item = lookup['lookups'][0]['results'][0]
        self.assertEqual(item['facts']['evidence_text'], html)
        self.assertEqual(item['facts']['requested_url'], URL)
        self.assertIsNone(item['facts']['evidence_url'])
        self.assertEqual(item['facts']['content_kind'], 'unverified')
        ref = item['ref']
        route_id = ref.split(':')[0]
        receipt = self.path.parent / 'receipts' / (route_id + '.json')
        before = (receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(),
                  len(self.provider.requests))
        detail = self.tools.inspect(ref=ref, field='evidence_text')
        self.assertEqual(detail['text'], html)
        with self.assertRaisesRegex(ValueError, 'no captured source body'):
            self.tools.review(companies=[self.finding(ref)])
        self.assertEqual((receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(),
                         len(self.provider.requests)), before)

    def test_scrapingdog_legacy_capture_does_not_override_explicit_classification(self):
        row = scrapingdog.normalize_result({'content': TEXT, 'target_url': URL}, 'scrape')
        saved = {'provider': 'scrapingdog', 'operation': 'scrape', 'tool': 'scrape',
                 'receipt_status': 'complete', 'status': 'ok', 'results': [row]}
        evidence = {'source': {k: saved[k] for k in ('provider', 'operation', 'tool')},
                    'url': URL, 'text': TEXT, 'date_basis': 'observed_current'}
        with patch.object(source_receipts, 'read_receipt', return_value={'result': saved}):
            source_receipts.web_passage(self.path, {}, evidence)
            del row['content_kind']  # Legacy adapter receipts predate this field.
            source_receipts.web_passage(self.path, {}, evidence)
            for kind in ('unverified', 'search_excerpt'):
                row['content_kind'] = kind
                with self.assertRaisesRegex(ValueError, 'no captured source body'):
                    source_receipts.web_passage(self.path, {}, evidence)

    def test_page_normalization_is_independent_of_tool_and_envelope(self):
        for tool in ('contextdev_post_web_crawl', 'firecrawl_scrape', 'another_page_reader'):
            for body in (response([page()]), {'results': [page()]}, [page()],
                         {'data': page()}, {'output_preview': {'rows': [page()]}}):
                with self.subTest(tool=tool, body=body):
                    saved = copy.deepcopy(body)
                    normalized, _ = deepline.normalize_response({'operation': 'execute', 'tool': tool,
                        'payload': {'url': URL}, 'limit': 10}, {'body': body, 'exit_code': 0, 'stderr': ''})
                    row = normalized['results'][0]
                    self.assertEqual((row['signal'], row['evidence_url'], row['evidence_text']),
                                     ('web_page', URL, TEXT))
                    self.assertEqual(source_date(row), ('2026-08-26', 'published'))
                    self.assertEqual(body, saved)

    def test_url_aliases_and_failed_pages(self):
        for key in ('sourceURL', 'sourceUrl', 'url', 'finalUrl'):
            row = page()
            row['metadata'] = {'statusCode': 200, key: URL}
            self.assertEqual(deepline.normalize_evidence(row)['evidence_url'], URL)
        for status in (404, 500, '200', True, None):
            row = page()
            row['metadata']['statusCode'] = status
            self.assertNotEqual(deepline.normalize_evidence(row)['signal'], 'web_page')
        row = page()
        row['success'] = False
        self.assertNotEqual(deepline.normalize_evidence(row)['signal'], 'web_page')
        row = page()
        row['metadata']['success'] = False
        self.assertNotEqual(deepline.normalize_evidence(row)['signal'], 'web_page')

    def test_explicit_success_without_http_status_qualifies_from_saved_body(self):
        row = page()
        row['success'] = True
        del row['metadata']['statusCode']
        del row['metadata']['success']
        ref = self.capture(row)
        before = budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[self.finding(ref)])
        document = json.loads(self.path.read_text())
        self.assertEqual(document['unresolved'][0]['stage'], 'contact')
        self.assertEqual(validate_run.qualification_errors(document, run_file=self.path), [])
        sources = {}
        self.tools._company_review(document['unresolved'][0], sources)
        self.assertEqual(sources[ref]['text'], TEXT)
        self.assertEqual((budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        for change in ({'success': False}, {'success': 'true'}, {'success': 1}, {'error': 'Failed capture'},
                       {'metadata': {**row['metadata'], 'statusCode': 500}},
                       {'metadata': {**row['metadata'], 'statusCode': None}},
                       {'metadata': {**row['metadata'], 'success': False}}, {'markdown': ''}):
            with self.subTest(change=change):
                self.assertNotEqual(deepline.normalize_evidence({**row, **change})['signal'], 'web_page')

    def test_capture_qualifies_and_binds_in_review_without_another_call(self):
        ref = self.capture()
        receipt = self.path.parent / 'receipts' / (ref.split(':')[0] + '.json')
        before = receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[self.finding(ref)])
        document = json.loads(self.path.read_text())
        row = document['unresolved'][0]
        self.assertEqual(row['stage'], 'contact')
        self.assertEqual(validate_run.qualification_errors(document, run_file=self.path), [])
        sources = {}
        packet = self.tools._company_review(row, sources)
        self.assertNotIn('source_error', json.dumps(packet))
        self.assertEqual(sources[ref]['url'], URL)
        self.assertEqual(sources[ref]['text'], TEXT)
        self.assertEqual(sources[ref]['date'], '2026-08-26')
        self.assertEqual((receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_forged_quote_url_or_date_is_rejected_before_account_qualification(self):
        ref = self.capture()
        before = self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for override in ({'text': 'The acquisition is merely planned.'},
                         {'url': 'https://example.test/other'}, {'date': '2026-08-25'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.tools.review(companies=[self.finding(ref, **override)])
            self.assertEqual((self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_saved_invalid_quote_blocks_existing_contact_gate_without_spend(self):
        ref = self.capture()
        self.tools.review(companies=[self.finding(ref)])
        document = json.loads(self.path.read_text())
        document['unresolved'][0]['qualification_checks'][0]['evidence'][0]['text'] = 'Invented claim.'
        self.path.write_text(json.dumps(document))
        before = budget_guard.ledger_path(self.path).read_bytes()
        paid = sum(r['operation'] == 'execute' for r in self.provider.requests)
        with self.assertRaisesRegex(ValueError, 'quote captured source'):
            self.tools.lookup([check(phase='contact_discovery', tool='fixture-search', inputs={'query': 'senior buyer'})])
        self.assertEqual(budget_guard.ledger_path(self.path).read_bytes(), before)
        self.assertEqual(sum(r['operation'] == 'execute' for r in self.provider.requests), paid)

    def test_failed_or_missing_body_cannot_be_replaced_with_authored_evidence(self):
        for change in ('failed', 'empty', 'missing', 'invalid_url', 'missing_status', 'missing_metadata'):
            row = page()
            if change == 'failed':
                row['metadata']['statusCode'] = 500
            elif change == 'empty':
                row['markdown'] = ''
            elif change == 'missing':
                del row['markdown']
            elif change == 'invalid_url':
                row['metadata'] = {'statusCode': 200, 'sourceUrl': 'not-a-url'}
            elif change == 'missing_status':
                del row['metadata']['statusCode']
            else:
                del row['metadata']
            ref = self.capture(row, url=URL + '?case=' + change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.tools.review(companies=[self.finding(ref, url=URL, text=TEXT,
                                                         date='2026-08-26', date_basis='published')])

    def test_structured_company_record_keeps_existing_path(self):
        self.provider.raw = {'status': 'ok', 'element': {'name': 'Example',
            'website': 'https://example.test', 'linkedinUrl': 'https://www.linkedin.com/company/example/',
            'employeeCountRange': {'start': 201, 'end': 500}}}
        ref = self.tools.lookup([check()])['lookups'][0]['results'][0]['ref']
        evidence = self.tools._evidence({'ref': ref, 'text': 'Company profile'})
        self.assertIsNone(validate_run.qualification_evidence_error(evidence, 'check',
            json.loads(self.path.read_text()), {}, {'status': 'pass'}, self.path))


if __name__ == '__main__':
    unittest.main()
