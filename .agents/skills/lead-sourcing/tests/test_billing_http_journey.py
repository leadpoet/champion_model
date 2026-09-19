"""Native calls and delayed billing through the real HTTP adapter, on localhost."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import unittest
from unittest.mock import patch

import test_research_tools as fixtures
import billing_reconciliation as billing
import budget_guard as budget
import deepline
import deepline_http


class BillingHttpJourneyTests(unittest.TestCase):
    setUp = fixtures.ResearchToolTests.setUp

    def journey(self, *, lose_body=False):
        requests, posted = [], [False]
        response = dict(copy.deepcopy(self.provider.raw), request_id='http-request-1')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, document, code=200, *, truncate=False):
                raw = json.dumps(document).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Deepline-Request-Id', 'http-request-1')
                self.send_header('Content-Length', str(len(raw) + (50 if truncate else 0)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                if truncate:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_WR)

            def do_POST(self):
                requests.append(('POST', self.path, self.headers.get('Authorization')))
                self.rfile.read(int(self.headers['Content-Length']))
                if sum(method == 'POST' for method, *_ in requests) == 1:
                    self.respond(response, truncate=lose_body)
                else:
                    self.respond({'error': {'message': 'Fixture input rejected'},
                                  'billing': {'credits_charged': 0, 'pricing_status': 'final'}}, 422)

            def do_GET(self):
                requests.append(('GET', self.path, self.headers.get('Authorization')))
                if '/ledger?' in self.path:
                    if not posted[0] and lose_body:
                        self.respond({'error': 'Fixture ledger outage'}, 503)
                    else:
                        rows = [{'id': 'http-debit-1', 'request_id': 'http-request-1',
                                 'provider': 'harvestapi', 'operation': 'harvestapi_get_company',
                                 'reason': 'charge_settle', 'billing_stage': 'posted', 'charge_state': 'posted',
                                 'charge_credits': .2, 'delta': -.2}] if posted[0] else []
                        self.respond({'org_id': 'fixture-org', 'entries': rows, 'has_more': False})
                else:
                    self.respond({'org_id': 'fixture-org', 'recent': {'entries': [], 'has_more': False}})

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        with patch.object(deepline_http, 'API_HOST', base), \
             patch.object(deepline_http, 'api_key', return_value='fixture-http-key'):
            # Free descriptions are fixtures; paid dispatch and billing reads
            # use the unmodified HTTP implementation and native persistence.
            self.tools.execute = lambda request, capture: (self.provider(request, capture)
                if request['operation'] != 'execute' else deepline.run(request, capture))
            self.tools.call('tyche_start', {'request': self.request, 'max_usd': .05})
            before = budget.load_ledger(self.path)
            started = json.loads(self.path.read_text())['stop_check']['started_at']
            first = self.tools.call('tyche_lookup', {'checks': [fixtures.check()]})['lookups'][0]
            rid = first['route']
            receipt_path = self.path.parent / 'receipts' / (rid + '.json')
            receipt = receipt_path.read_bytes()
            self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), 'billing_pending')
            pending = billing.reconcile(self.path)
            self.assertEqual(pending['unmatched'], [rid])
            self.assertEqual(sum(method == 'POST' for method, *_ in requests), 1)
            self.assertTrue(any('/usage?' in path for _, path, _ in requests))
            posted[0] = True
            settled = billing.reconcile(self.path, refresh=True)
            self.assertEqual(settled['unmatched'], [])
            self.assertEqual(budget.load_ledger(self.path)['calls'][rid]['actual_credits'], '0.2')
            self.assertEqual(receipt_path.read_bytes(), receipt)
            self.assertIsNone(budget.spending_stop(budget.load_ledger(self.path)))
            self.tools.call('tyche_lookup', {'checks': [fixtures.check('next.test')]})
            final = budget.load_ledger(self.path)
            costs = self.tools.call('tyche_inspect', {'field': 'costs'})['costs']
            self.assertEqual((costs['provider_usd'], costs['pending_provider_calls']), (.02, 0))
            self.assertEqual(final['usd_limit'], before['usd_limit'])
            self.assertEqual(json.loads(self.path.read_text())['stop_check']['started_at'], started)
            self.assertEqual(sum(method == 'POST' for method, *_ in requests), 2)
            self.assertTrue(all(auth == 'Bearer fixture-http-key' for _, _, auth in requests))
            self.assertNotIn('fixture-http-key', receipt.decode())
            self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
            if lose_body:
                saved = json.loads(receipt)
                self.assertEqual(saved['request_id'], 'http-request-1')
                self.assertEqual(saved['provider_response']['transport']['stage'], 'reading_response')
                self.assertIn(first['status'], deepline._FAILURE_STATUSES)

    def test_success_with_late_billing_and_explicit_free_rejection(self):
        self.journey()

    def test_lost_body_and_billing_outage_recover_without_paid_replay(self):
        self.journey(lose_body=True)
