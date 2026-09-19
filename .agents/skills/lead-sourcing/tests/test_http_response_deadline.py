"""Real local HTTP responses cannot extend their budget by trickling bytes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deepline_http as http
import deepline
import budget_guard as budget


class ResponseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.mode, self.requests = 'body', []
        case = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.reply('POST')

            def do_GET(self):
                self.reply('GET')

            def reply(self, method):
                case.requests.append((method, self.path))
                mode = case.mode
                raw = json.dumps({'entries': [], 'billing': {
                    'credits_charged': .02, 'pricing_status': 'final'}}).encode()
                try:
                    if mode == 'headers':
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                        for _ in range(20):
                            self.wfile.write(b'a'); self.wfile.flush(); time.sleep(.03)
                        self.wfile.write(b'\r\nContent-Length: 0\r\n\r\n')
                        return
                    self.send_response(200)
                    self.send_header('X-Deepline-Request-Id', 'deadline-fixture')
                    if mode == 'chunked':
                        self.send_header('Transfer-Encoding', 'chunked')
                    else:
                        self.send_header('Content-Length', str(len(raw) + (10 if mode == 'truncated' else 0)))
                    self.end_headers()
                    if mode == 'chunked':
                        # Even a slowly arriving chunk header must share the budget.
                        for byte in b'00000000000000000001\r\nx\r\n0\r\n\r\n':
                            self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.03)
                    elif mode == 'body':
                        for i in range(0, len(raw), 4):
                            self.wfile.write(raw[i:i + 4]); self.wfile.flush(); time.sleep(.03)
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host = patch.object(http, 'API_HOST', f'http://127.0.0.1:{self.server.server_port}')
        host.start()
        self.addCleanup(host.stop)
        self.request = {'tool': 'fixture_tool', 'payload': {}, 'timeout_seconds': .15}

    def assert_deadline(self, mode):
        self.mode = mode
        started = time.monotonic()
        result = http.execute(self.request, key='fixture-secret')
        self.assertLess(time.monotonic() - started, .45)
        self.assertTrue(result.get('timed_out'))
        self.assertEqual(result['body'], '')
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(len(self.requests), 1)  # No paid retry.
        if mode != 'headers':
            self.assertEqual(result['headers']['x-deepline-request-id'], 'deadline-fixture')
            self.assertEqual(result['transport']['stage'], 'reading_response')
        started = time.monotonic()
        with self.assertRaises(http.BillingUnavailable):
            http.billing_page('ledger', key='fixture-secret', timeout=.15)
        self.assertLess(time.monotonic() - started, .45)
        self.assertEqual([method for method, _ in self.requests], ['POST', 'GET'])

    def test_slow_body_uses_one_response_deadline(self):
        self.assert_deadline('body')

    def test_slow_headers_uses_one_response_deadline(self):
        self.assert_deadline('headers')

    def test_slow_chunk_metadata_uses_one_response_deadline(self):
        self.assert_deadline('chunked')

    def test_normal_and_truncated_responses_keep_billing_and_identity(self):
        self.mode = 'normal'
        response = http.execute(self.request, key='fixture-secret')
        self.assertEqual(response['body']['billing']['credits_charged'], .02)
        self.assertEqual(response['exit_code'], 0)
        self.mode = 'truncated'
        response = http.execute(self.request, key='fixture-secret')
        self.assertEqual(response['body'], '')
        self.assertEqual(response['headers']['x-deepline-request-id'], 'deadline-fixture')
        self.assertEqual(response['transport']['error_type'], 'IncompleteRead')
        self.assertEqual(len(self.requests), 2)

    def test_https_retains_default_certificate_and_hostname_verification(self):
        handler = http._DeadlineHTTPSHandler(time.monotonic() + 1)
        if handler._context is None:
            # urllib passes both sentinels through to HTTPSConnection, which
            # creates its verified default context and enables hostname checks.
            self.assertIsNone(handler._check_hostname)
        else:
            self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(handler._context.check_hostname)

    def test_timed_out_body_stays_pending_in_the_actual_cost_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            path.write_text(json.dumps({'request': {'target_count': 1, 'contact_fields': []},
                'accepted': [], 'routes': [], 'budget': {'policy': 'actual_cost', 'paid_calls': 0,
                    'limits': {'deepline_credits': 10, 'scrapingdog_credits': 0}}}))
            budget.initialize(path, max_usd=1)
            request = dict(self.request, operation='execute',
                           spend={'run_file': str(path), 'route_id': 'slow-call'})
            captured = []
            with patch.object(http, 'api_key', return_value='fixture-secret'):
                result, _ = deepline.run(request, capture=captured.append)
            self.assertEqual(result['status'], 'timeout')
            self.assertEqual(result['request_id'], 'deadline-fixture')
            self.assertNotIn('billing', result)
            ledger = budget.load_ledger(path)
            self.assertEqual(ledger['calls']['slow-call']['state'], 'pending_billing')
            self.assertIsNone(ledger['calls']['slow-call']['actual_credits'])
            self.assertEqual(budget.spending_stop(ledger), 'billing_pending')
            self.assertEqual(len(captured), 1)
            self.assertEqual(len(self.requests), 1)
