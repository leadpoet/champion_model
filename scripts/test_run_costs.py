import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from run_costs import UsageJournal, UsageReceipt, estimate, execute_with_usage, report, save_report


class RunCostsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.request = self.root / 'request.txt'
        self.request.write_text('Synthetic no-provider test.')
        self.thread = str(uuid.uuid4())
        self.usage = dict(input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0,
                          output_tokens=100, reasoning_output_tokens=50, total_tokens=1100)

    def receipt(self):
        receipt = UsageReceipt(self.request, 'gpt-5.6-luna', 'xhigh', 'fast')
        receipt.observe({'type': 'thread.started', 'thread_id': self.thread})
        return receipt

    def response(self, usage=None, response_id='response-1'):
        return {'type': 'token_usage_record', 'timestamp': '2026-09-13T00:00:00Z',
                'payload': {'thread_id': self.thread, 'turn_id': 'turn-1', 'response_id': response_id,
                            'usage': usage or self.usage}}

    def record_response(self, receipt, usage=None, response_id='response-1'):
        record = self.response(usage, response_id)
        receipt.observe_response(record['payload'], record['timestamp'], 'gpt-5.6-luna')

    def completed(self):
        receipt = self.receipt()
        self.record_response(receipt)
        receipt.observe({'type': 'turn.completed', 'usage': self.usage})
        receipt.finish(0)
        return receipt

    def journal_path(self):
        path = self.root / 'profile' / 'sessions' / '2026' / '09' / '13' / ('rollout-' + self.thread + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def results(self, high=0.831):
        return {'accepted': [{}]*5, 'cost_summary': {'deepline': {'confirmed_usd': 0.831, 'maximum_usd': high},
                                                  'scrapingdog': {'maximum_credits': 0}}}

    def test_caching_writes_output_and_context_are_priced_separately(self):
        self.assertEqual(estimate(self.usage, 'gpt-5.6-luna', per_request=True),
                         0.000176)
        usage = dict(input_tokens=300000, cached_input_tokens=200000, cache_write_input_tokens=10000,
                     output_tokens=10000, total_tokens=310000)
        self.assertEqual(estimate(usage, 'gpt-5.6-luna', per_request=True), 0.067)
        usage.pop('cache_write_input_tokens')
        self.assertEqual(estimate(usage, 'gpt-5.6-luna'), 0.036)
        for bad in ({'input_tokens': -1}, dict(self.usage, cached_input_tokens=1001), dict(self.usage, total_tokens=1)):
            with self.assertRaises(ValueError):
                estimate(bad, 'gpt-5.6-luna')

    def test_worker_failure_category_does_not_copy_error_payload(self):
        receipt = self.receipt()
        receipt.observe({'type': 'turn.failed', 'error': {'message': 'Usage limit reached; private account detail'}})
        self.assertEqual(receipt.data['failure_kind'], 'model_usage_limit')
        self.assertNotIn('private account detail', receipt.path.read_text())

    def test_disconnected_stdout_preserves_usage_and_worker_completion(self):
        receipt = self.receipt()
        class Disconnected(io.StringIO):
            def write(self, value):
                raise BrokenPipeError('observer left')
        event = json.dumps({'type': 'turn.completed', 'usage': self.usage})
        with contextlib.redirect_stdout(Disconnected()):
            execute_with_usage([sys.executable, '-c', 'print(' + repr(event) + ')'],
                               self.root, os.environ.copy(), receipt)
        self.assertIsNotNone(receipt.data['finished_at'])
        self.assertGreater(receipt.data['process_group_id'], 1)
        self.assertEqual(receipt.data['exit_code'], 0)
        self.assertEqual(receipt.data['usage'], self.usage)

    def test_watchdog_stops_a_silent_worker_and_its_pipe_holding_descendant(self):
        receipt = self.receipt()
        program = ('import subprocess,sys,time; '
                   'subprocess.Popen([sys.executable,"-c","import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]); '
                   'time.sleep(60)')
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            code = execute_with_usage([sys.executable, '-c', program], self.root, os.environ.copy(), receipt,
                                      deadline=lambda: time.time() - 1)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(receipt.data['failure_kind'], 'deadline_reached')
        self.assertIsNotNone(receipt.data['finished_at'])

    def test_live_journal_cost_stops_silent_worker_without_waiting_for_final_usage(self):
        receipt = self.receipt()
        path = self.journal_path()
        records = [{'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna'}}, self.response()]
        program = ('import time; from pathlib import Path; Path(' + repr(str(path)) + ').write_text('
            + repr(''.join(json.dumps(r)+'\n' for r in records)) + '); time.sleep(60)')
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            code = execute_with_usage([sys.executable, '-c', program], self.root, os.environ.copy(), receipt,
                profile=self.root / 'profile',
                cost_stop=lambda: 'budget_exhausted' if (receipt.data['estimated_base_usd'] or 0) >= .0001 else None)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(receipt.data['failure_kind'], 'budget_exhausted')
        self.assertEqual(receipt.data['estimated_base_usd'], .000176)
        self.assertEqual(receipt.data['status'], 'incomplete')

    def test_missing_legacy_plan_conversion_never_reports_complete_cost(self):
        receipt = self.completed()
        results = self.results()
        results['cost_summary']['scrapingdog'] = {'confirmed_credits': 100}
        costs = report(results, [receipt.path], self.root)
        self.assertEqual(costs['status'], 'incomplete')
        self.assertEqual(costs['total_usd'], .831176)
        self.assertTrue(any('conversion' in issue for issue in costs['missing']))

    def test_invalid_deadline_state_fails_closed(self):
        receipt = self.receipt()
        def deadline():
            raise ValueError('invalid saved duration')
        with contextlib.redirect_stdout(io.StringIO()):
            execute_with_usage([sys.executable, '-c', 'import time; time.sleep(60)'], self.root,
                               os.environ.copy(), receipt, deadline=deadline)
        self.assertEqual(receipt.data['failure_kind'], 'invalid_saved_state')

    def test_cancellation_terminates_worker_and_preserves_receipt(self):
        receipt = self.receipt()
        with patch.object(receipt, 'observe', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                execute_with_usage([sys.executable, '-c', 'import time; print("{}", flush=True); time.sleep(60)'],
                                   self.root, os.environ.copy(), receipt)
        self.assertEqual(receipt.data['failure_kind'], 'cancelled')
        self.assertIsNotNone(receipt.data['finished_at'])

    def test_aggregate_input_does_not_trigger_per_request_long_context_rates(self):
        receipt = self.receipt()
        usage = dict(self.usage, input_tokens=200000, cached_input_tokens=100000, total_tokens=200100)
        self.record_response(receipt, usage, 'first')
        self.record_response(receipt, usage, 'second')
        receipt.observe({'type': 'turn.completed', 'usage': {k: v*2 for k, v in usage.items()}})
        receipt.finish(0)
        self.assertEqual(receipt.data['status'], 'complete')
        self.assertEqual(receipt.data['estimated_base_usd'], 0.04424)

    def test_real_process_stream_and_journal_capture_only_numeric_metadata(self):
        receipt = self.receipt()
        path = self.journal_path()
        records = [{'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna', 'instructions': 'private prompt'}},
                   {'type': 'response_item', 'payload': {'text': 'private tool data'}}, self.response()]
        events = [{'type': 'thread.started', 'thread_id': self.thread},
                  {'type': 'item.completed', 'item': {'text': 'private tool data'}},
                  {'type': 'turn.completed', 'usage': self.usage}]
        program = ('import json; from pathlib import Path; Path(' + repr(str(path)) + ').write_text('
                   + repr(''.join(json.dumps(r)+'\n' for r in records)) + '); events=' + repr(events)
                   + '; [print(json.dumps(e)) for e in events]')
        with contextlib.redirect_stdout(io.StringIO()):
            code = execute_with_usage([sys.executable, '-c', program], self.root, os.environ.copy(),
                                      receipt, profile=self.root / 'profile')
        self.assertEqual(code, 0)
        saved = json.loads(receipt.path.read_text())
        self.assertEqual(saved['status'], 'complete')
        self.assertTrue(saved['usage_reconciled'])
        self.assertEqual(saved['response_usage_totals'], self.usage)
        self.assertIsNone(saved['actual_model_billed_usd'])
        self.assertNotIn('private prompt', receipt.path.read_text())
        self.assertNotIn('private tool data', receipt.path.read_text())

    def test_large_and_partial_journal_rows_are_skipped_or_retried(self):
        receipt = self.receipt()
        path = self.journal_path()
        row = json.dumps(self.response()).encode() + b'\n'
        path.write_bytes(json.dumps({'type': 'response_item', 'payload': {'text': 'x'*200000}}).encode()
                         + b'\n' + row[:70])
        journal = UsageJournal(self.root / 'profile', receipt)
        journal.poll()
        self.assertEqual(receipt.data['responses'], [])
        with path.open('ab') as stream:
            stream.write(row[70:])
        journal.poll()
        journal.poll()
        self.assertEqual(len(receipt.data['responses']), 1)

    def test_duplicate_conflicting_and_cross_worker_records(self):
        receipt = self.receipt()
        self.record_response(receipt)
        self.record_response(receipt)
        self.assertEqual(len(receipt.data['responses']), 1)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.record_response(receipt, dict(self.usage, output_tokens=101, total_tokens=1101))
        record = self.response()['payload']
        record['thread_id'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'another worker'):
            receipt.observe_response(record, None, 'gpt-5.6-luna')

    def test_missing_or_mismatched_usage_remains_incomplete(self):
        first, second = self.receipt(), self.receipt()
        self.assertNotEqual(first.path, second.path)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute_with_usage([sys.executable, '-c', 'raise SystemExit(2)'], self.root,
                                                os.environ.copy(), first), 2)
        self.assertEqual(first.data['status'], 'incomplete')
        self.assertIsNone(first.data['estimated_base_usd'])
        self.record_response(second)
        second.observe({'type': 'turn.completed', 'usage': dict(self.usage, input_tokens=1100, total_tokens=1200)})
        second.finish(0)
        self.assertFalse(second.data['usage_reconciled'])
        self.assertEqual(second.data['status'], 'incomplete')

    def test_interrupted_attempt_retains_completed_responses_without_claiming_full_cost(self):
        receipt = self.receipt()
        self.record_response(receipt)
        receipt.finish(130)
        self.assertEqual(receipt.data['estimated_base_usd'], 0.000176)
        self.assertEqual(receipt.data['status'], 'incomplete')
        self.assertEqual(report(self.results(), [receipt.path], self.root)['status'], 'incomplete')
        self.assertEqual(report(self.results(), [receipt.path], self.root)['total_usd'], 0.831176)

    def test_partial_final_usage_cannot_pass_reconciliation(self):
        receipt = self.receipt()
        self.record_response(receipt)
        receipt.observe({'type': 'turn.completed', 'usage': {'input_tokens': 1000}})
        receipt.finish(0)
        self.assertFalse(receipt.data['usage_reconciled'])
        self.assertEqual(receipt.data['status'], 'incomplete')

    def test_rerouted_model_remains_incomplete(self):
        receipt = self.receipt()
        path = self.journal_path()
        path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'model_rerouted'}}) + '\n')
        with self.assertRaisesRegex(ValueError, 'rerouted'):
            UsageJournal(self.root / 'profile', receipt).poll()

    def test_report_cli_saves_the_same_report_it_prints(self):
        import subprocess
        self.completed()
        results = self.root / 'results.json'
        results.write_text(json.dumps(self.results()))
        executed = subprocess.run([sys.executable, str(Path(__file__).with_name('run_costs.py')), str(results)],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(executed.stdout), json.loads((self.root / 'run-costs.json').read_text()))

    def test_telemetry_error_does_not_interrupt_worker(self):
        receipt = self.receipt()
        event = {'type': 'turn.completed', 'usage': self.usage}
        marker = self.root / 'worker-finished'
        program = ('import json; from pathlib import Path; event=' + repr(event)
                   + '; print(json.dumps(event)); print(json.dumps(event)); Path(' + repr(str(marker)) + ').touch()')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute_with_usage([sys.executable, '-c', program], self.root,
                                                os.environ.copy(), receipt), 2)
        self.assertTrue(marker.exists())
        self.assertEqual(receipt.data['exit_code'], 0)
        self.assertEqual(receipt.data['status'], 'incomplete')

    def test_run_total_automatically_includes_every_attempt_and_excludes_outer_chat(self):
        missing = report(self.results(), [])
        self.assertEqual(missing['status'], 'incomplete')
        self.assertEqual(missing['total_usd'], 0.831)
        receipt = self.completed()
        (self.root / 'results.json').write_text(json.dumps(self.results()))
        saved = json.loads(save_report(self.root).read_text())
        self.assertEqual(saved['scope'], 'tyche_run_only')
        self.assertNotIn('monitoring', saved)
        self.assertEqual(saved['status'], 'calculated')
        self.assertEqual(saved['total_usd'], 0.831176)
        self.assertEqual(saved['cost_per_accepted_lead_usd'], 0.1662352)
        ranged = report(self.results(0.845), [receipt.path], self.root)
        self.assertEqual(ranged['status'], 'incomplete')
        self.assertEqual(ranged['total_usd'], 0.831176)
        self.assertIsNone(ranged['cost_per_accepted_lead_usd'])
        self.receipt().finish(1)
        self.assertEqual(json.loads(save_report(self.root).read_text())['status'], 'incomplete')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            report(self.results(), [receipt.path, receipt.path], self.root)
        with self.assertRaisesRegex(ValueError, 'different run'):
            report(self.results(), [receipt.path], self.root / 'another-run')

    def test_native_report_refreshes_costs_without_replacing_research_or_results(self):
        results = self.results()
        results.update(request={'target_count': 5}, stop_reason='target_met',
                       stop_check={'started_at':'2026-09-13T00:00:00Z', 'leads_ready_at':'2026-09-13T00:24:10Z'})
        result_path = self.root / 'results.json'
        result_path.write_text(json.dumps(results))
        (self.root / 'validation.json').write_text(json.dumps({'completed_at':'2026-09-13T00:25:30Z'}))
        (self.root / 'research-commentary.md').write_text('Reviewed signals and contact selection. Fixture only.')
        before = result_path.read_bytes()
        save_report(self.root)
        first = (self.root / 'report.md').read_text()
        self.assertIn('25m 30s', first)
        self.assertIn('Sourcing model usage was not captured', first)
        self.completed()
        save_report(self.root)
        final = (self.root / 'report.md').read_text()
        self.assertIn('Reviewed signals and contact selection. Fixture only.', final)
        self.assertNotIn('Sourcing model usage was not captured', final)
        self.assertIn('$0.8312', final)
        self.assertEqual(result_path.read_bytes(), before)

    def test_report_uses_ledger_for_interrupted_calls_and_reconciled_costs(self):
        import hashlib
        self.completed()
        result_path = self.root / 'results.json'
        # A saved route summary can lag both dispatch and later billing reconciliation.
        result_path.write_text(json.dumps(self.results()))
        ledger = dict(version=1, run_file=str(result_path.resolve()),
                      run_fingerprint=hashlib.sha256(str(result_path.resolve()).encode()).hexdigest(),
                      usd_per_credit={'deepline': '0.10', 'scrapingdog': '0.001'},
                      verification_reserve_credits='1.4', calls={
                          'settled': dict(provider='deepline', actual_credits='2.4',
                                          actual_usd='0.24', maximum_credits='2.4'),
                          'interrupted': dict(provider='deepline', actual_credits=None,
                                              actual_usd=None, maximum_credits='0.28')})
        ledger_path = result_path.with_name('results.json.budget.json')
        before = result_path.read_bytes()
        for settled in (False, True):
            with self.subTest(settled=settled):
                if settled:
                    ledger['calls']['interrupted'].update(actual_credits='0', actual_usd='0')
                ledger_path.write_text(json.dumps(ledger))
                ledger_before = ledger_path.read_bytes()
                costs = json.loads(save_report(self.root).read_text())
                self.assertEqual(costs['provider_usd'], 0.24)
                self.assertEqual(costs['status'], 'calculated' if settled else 'incomplete')
                self.assertEqual(costs['pending_provider_calls'], 0 if settled else 1)
                self.assertEqual(costs['total_usd'], 0.240176)
                self.assertEqual(costs['cost_per_accepted_lead_usd'], 0.0480352 if settled else None)
                self.assertEqual(result_path.read_bytes(), before)
                self.assertEqual(ledger_path.read_bytes(), ledger_before)
        # The same ledger supplies ScrapingDog's saved plan conversion.
        ledger['calls']['scrape'] = dict(provider='scrapingdog', actual_credits='100',
                                       actual_usd=None, maximum_credits='100')
        ledger_path.write_text(json.dumps(ledger))
        costs = json.loads(save_report(self.root).read_text())
        self.assertEqual(costs['provider_usd'], 0.34)
        self.assertEqual(costs['total_usd'], 0.340176)
        ledger['run_file'] = str(self.root / 'another-run.json')
        ledger_path.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(ValueError, 'different run'):
            save_report(self.root)

    def test_explicit_compaction_link_explains_cli_total_but_all_responses_are_priced(self):
        receipt=self.receipt()
        self.record_response(receipt,response_id='ordinary')
        self.record_response(receipt,response_id='compact')
        receipt.observe_compaction('compact')
        receipt.observe({'type':'turn.completed','usage':self.usage})
        receipt.finish(0)
        self.assertEqual(receipt.data['status'],'complete')
        self.assertEqual(receipt.data['reconciliation_basis'],'cli_excludes_compaction')
        self.assertEqual(receipt.data['estimated_base_usd'],0.000352)
        self.assertEqual(receipt.data['compaction_usage_totals'],self.usage)

    def test_guessing_or_missing_compaction_usage_cannot_reconcile(self):
        for link in (None,'missing'):
            receipt=self.receipt();self.record_response(receipt,response_id='ordinary')
            self.record_response(receipt,response_id='unexplained')
            if link:receipt.observe_compaction(link)
            receipt.observe({'type':'turn.completed','usage':self.usage});receipt.finish(0)
            self.assertEqual(receipt.data['status'],'incomplete')

    def test_large_partial_compaction_record_keeps_only_linkage(self):
        receipt=self.receipt();path=self.journal_path()
        record={'type':'compacted','payload':{'message':'private '*20000,
                'replacement_history':[{'text':'private history'}], 'compaction_response_id':'compact'}}
        raw=(json.dumps(record)+'\n').encode()
        path.write_bytes(raw[:90000]);journal=UsageJournal(self.root/'profile',receipt)
        journal.poll();self.assertEqual(receipt.data['compaction_response_ids'],[])
        with path.open('ab') as f:f.write(raw[90000:])
        journal.poll();journal.poll()
        self.assertEqual(receipt.data['compaction_response_ids'],['compact'])
        self.assertNotIn('private history',receipt.path.read_text())
        self.assertNotIn('private private',receipt.path.read_text())


if __name__ == '__main__':
    unittest.main()
