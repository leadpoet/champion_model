"""Reduced native page replies from the failed cybersecurity run, with synthetic content."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_provider_scripts import DEEPLINE as deepline
import budget_guard as budget

URL = 'https://example.test/funding'
TEXT = 'Example announced Series B funding on September 17, 2026.'


def native_page_response(tool, url=URL, text=TEXT):
    raw = {'language': 'en', 'text': text} if tool == 'discolike_extract' else {
        'provider': 'generic_http', 'operation': 'generic_http_request', 'method': 'GET',
        'requested_url': url, 'final_url': url, 'status_code': 200, 'ok': True,
        'headers': {'content-type': 'text/html; charset=utf-8'}, 'data': '<p>' + text + '</p>'}
    return {'status': 'completed', 'job_id': 'fixture-page-request', 'toolResponse': {'rawV2': raw}}


class PageReaderResponseTests(unittest.TestCase):
    def normalize(self, tool, body, payload=None, **transport):
        return deepline.normalize_response({'operation': 'execute', 'tool': tool,
            'payload': {'url': URL} if payload is None else payload, 'limit': 10},
            {'exit_code': 0, 'body': body, **transport})[0]

    def test_observed_forms_preserve_url_body_identity_billing_and_original(self):
        for tool, content_format in [('discolike_extract', 'text'), ('generic_http_request', 'html')]:
            with self.subTest(tool=tool):
                body = native_page_response(tool)
                body['billing'] = {'cost_usd': .3, 'credits_charged': 3}
                before = copy.deepcopy(body)
                result = self.normalize(tool, body)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['job_id'], body['job_id'])
                self.assertEqual(result['billing'], body['billing'])
                row, = result['results']
                self.assertEqual(row['content_kind'], 'captured_page')
                self.assertEqual(row['signal'], 'web_page')
                self.assertEqual(row['content_format'], content_format)
                self.assertEqual(row['evidence_url'], URL)
                self.assertEqual(row['evidence_text'], TEXT if content_format == 'text' else '<p>' + TEXT + '</p>')
                self.assertIsNone(row['evidence_date'])  # A date in prose is not publication metadata.
                self.assertIsNone(row['company'])
                self.assertEqual(body, before)

    def test_http_redirect_and_plain_text_preserve_final_source_url(self):
        body = native_page_response('generic_http_request')
        raw = body['toolResponse']['rawV2']
        raw.update(final_url='https://example.test/news/funding', data=TEXT,
                   headers={'Content-Type': 'text/plain; charset=UTF-8'})
        row = self.normalize('generic_http_request', body)['results'][0]
        self.assertEqual(row['evidence_url'], raw['final_url'])
        self.assertEqual(row['content_format'], 'text')
        self.assertEqual(row['evidence_text'], TEXT)

    def test_failure_uncertainty_and_wrong_tools_do_not_become_captured_pages(self):
        for tool in ['discolike_extract', 'generic_http_request']:
            original = native_page_response(tool)
            for change in [{'status': 'failed'}, {'error': {'message': 'Denied'}},
                           {'status': 'running'}, {'ok': False}, {'success': False}]:
                with self.subTest(tool=tool, change=change):
                    result = self.normalize(tool, {**original, **change})
                    self.assertFalse(any(r.get('content_kind') == 'captured_page' for r in result.get('results', [])))
            for change in [{'ok': False}, {'success': False}, {'status': 'failed'}, {'error': 'Denied'},
                           {'status': 'running'}, {'status': 'queued'}, {'status': 'partial'}]:
                for level in ('toolResponse', 'rawV2'):
                    with self.subTest(tool=tool, level=level, change=change):
                        body = copy.deepcopy(original)
                        target = body['toolResponse'] if level == 'toolResponse' else body['toolResponse']['rawV2']
                        target.update(change)
                        result = self.normalize(tool, body)
                        self.assertFalse(any(r.get('content_kind') == 'captured_page' for r in result.get('results', [])))
            for transport in [{'timed_out': True}, {'exit_code': 1, 'stderr': 'failed'}]:
                with self.subTest(tool=tool, transport=transport):
                    result = self.normalize(tool, original, **transport)
                    self.assertNotEqual(result['status'], 'ok')
                    self.assertFalse(result.get('results'))
            other = self.normalize('other_tool', original)
            self.assertFalse(any(r.get('content_kind') == 'captured_page' for r in other.get('results', [])))

    def test_invalid_http_source_status_or_content_does_not_become_evidence(self):
        for change in [{'status_code': 403}, {'status_code': '200'}, {'status_code': True},
                       {'ok': False}, {'error': 'fetch failed'}, {'method': 'POST'},
                       {'provider': 'other'}, {'operation': 'other'},
                       {'requested_url': 'https://other.test'}, {'final_url': 'https://[broken'},
                       {'final_url': ''}, {'data': ''}, {'data': {'company': 'Example'}},
                       {'headers': {'content-type': 'application/json'}}]:
            with self.subTest(change=change):
                body = native_page_response('generic_http_request')
                body['toolResponse']['rawV2'].update(change)
                result = self.normalize('generic_http_request', body)
                self.assertFalse(any(r.get('content_kind') == 'captured_page' for r in result.get('results', [])))

    def test_http_requires_a_valid_request_url_even_when_final_url_is_valid(self):
        for url in [None, '', 'relative/path', 'file:///tmp/source', 'https://', 'https://[broken']:
            with self.subTest(url=url):
                body = native_page_response('generic_http_request')
                body['toolResponse']['rawV2']['requested_url'] = url
                result = self.normalize('generic_http_request', body, {'url': url})
                self.assertFalse(any(r.get('content_kind') == 'captured_page' for r in result.get('results', [])))

    def test_extractor_requires_known_shape_nonempty_text_and_request_url(self):
        for raw in [{'language': 'en', 'text': ''}, {'language': 'en', 'text': None},
                    {'language': 'en', 'text': TEXT, 'error': 'failed'}, {'text': TEXT}]:
            with self.subTest(raw=raw):
                body = native_page_response('discolike_extract')
                body['toolResponse']['rawV2'] = raw
                self.assertFalse(self.normalize('discolike_extract', body).get('results'))
        for payload in [{}, {'url': 'file:///tmp/a'}, {'url': 'https://[broken'}]:
            with self.subTest(payload=payload):
                result = self.normalize('discolike_extract', native_page_response('discolike_extract'), payload)
                self.assertFalse(result.get('results'))

    def test_extra_extractor_metadata_does_not_discard_a_successful_page(self):
        body = native_page_response('discolike_extract')
        body['toolResponse']['rawV2']['word_count'] = 100
        self.assertEqual(self.normalize('discolike_extract', body)['results'][0]['evidence_text'], TEXT)

    def test_cli_and_saved_replay_agree_without_redispatch(self):
        for tool in ['discolike_extract', 'generic_http_request']:
            body = native_page_response(tool)
            captured = []
            request = {'operation': 'execute', 'tool': tool, 'payload': {'url': URL}, 'limit': 10}
            with patch.object(deepline, '_invoke', return_value=(0, json.dumps(body), '')) as dispatch:
                live = deepline._run_command(request, ['fixture'], 10, captured.append)
                replay = deepline.normalize_response(request, captured[0])
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(live, replay)
            self.assertEqual(captured[0]['body'], body)

    def test_successful_content_with_unknown_billing_still_pauses_paid_work(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'results.json'
            path.write_text(json.dumps({'run_id': 'fixture', 'request': {'target_count': 5},
                'accepted': [], 'routes': [], 'budget': {'policy': 'actual_cost', 'paid_calls': 0,
                'limits': {'deepline_credits': 25, 'scrapingdog_credits': 0}}}))
            budget.initialize(path, max_usd=2.5)
            body = native_page_response('discolike_extract')
            result, _ = budget.guarded_call({'spend': {'run_file': str(path), 'route_id': 'page'}},
                'deepline', lambda: (self.normalize('discolike_extract', body), 0))
            self.assertEqual(result['status'], 'ok')
            state = budget.load_ledger(path)
            self.assertEqual(state['calls']['page']['state'], 'pending_billing')
            self.assertIsNone(state['calls']['page']['actual_credits'])
            self.assertEqual(budget.spending_stop(state), 'billing_pending')
            budget.guarded_call({'spend': {'run_file': str(path), 'route_id': 'next'}},
                'deepline', lambda: self.fail('Unknown billing must pause dispatch'))
