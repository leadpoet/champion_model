"""Billing recovery shares ownership, retry history and the existing deadline."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import test_billing_reconciliation as fixtures
import billing_reconciliation as billing
import budget_guard as budget
import run_coordination as coordination


class BillingConcurrencyTests(unittest.TestCase):
    setUp = fixtures.BillingReconciliationTests.setUp

    def test_concurrent_recovery_cannot_duplicate_reads_or_reset_attempt_history(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        before = budget.ledger_path(self.path).read_bytes(), self.receipt_path.read_bytes()

        def fetch():
            calls.append('read')
            entered.set()
            self.assertTrue(release.wait(5))
            return {'recent': {'entries': []}}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(billing.reconcile, self.path, fetch=fetch, refresh=True)
            try:
                self.assertTrue(entered.wait(5))
                started = time.monotonic()
                # Native finalization can already own the state lock. Recovery
                # must not wait for a peer that needs that lock to commit.
                with coordination.locked(self.path):
                    second = billing.reconcile(self.path, fetch=self.fail, refresh=True, resume=True)
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(second['in_progress'])
                self.assertEqual(second['attempts'], 1)
                self.assertNotIn('attempt_limit', second)
            finally:
                release.set()
            first.result(timeout=5)
        saved = json.loads((self.path.parent / 'billing-status.json').read_text())
        self.assertEqual((len(calls), saved['attempts'], len(saved['read_attempts'])), (1, 1, 1))
        self.assertNotIn('in_progress', saved)  # Ownership is live, never a stale saved flag.
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), self.receipt_path.read_bytes()), before)
        final = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}}, refresh=True)
        self.assertEqual(final['attempts'], 2)
        self.assertEqual(final['unmatched'], [])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_crashed_recovery_owner_releases_across_processes_without_reset(self):
        marker = self.path.parent / 'owner-entered'
        program = """
import pathlib,sys,time
import billing_reconciliation as billing
import deepline_http
deepline_http.api_key = lambda: None
def fetch():
    pathlib.Path(sys.argv[2]).touch()
    time.sleep(30)
    return {'recent': {'entries': []}}
billing.reconcile(sys.argv[1], fetch=fetch, refresh=True)
"""
        child = subprocess.Popen([sys.executable, '-c', program, str(self.path), str(marker)],
            env=dict(os.environ, PYTHONPATH=str(Path(billing.__file__).parent)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            current = billing.reconcile(self.path, fetch=self.fail, refresh=True)
            self.assertTrue(current['in_progress'])
            self.assertEqual(current['attempts'], 1)
        finally:
            child.kill()
            child.communicate(timeout=5)
        final = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}}, refresh=True)
        self.assertEqual(final['attempts'], 2)
        self.assertEqual(len(final['read_attempts']), 2)
        self.assertEqual(final['unmatched'], [])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_wait_for_owner_uses_only_local_progress_and_preserves_deadline(self):
        fixtures.BillingReconciliationTests.actual_cost_pending(self)
        clock = [1000.0]
        with patch.object(billing.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(billing, 'reconcile', return_value={'in_progress': True}) as read:
            billing.wait_for_billing(self.path, deadline=1002.5)
        self.assertEqual(clock[0], 1002.5)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), 'billing_pending')

    def test_busy_state_lock_returns_without_waiting_or_consuming_a_read(self):
        entered, release = threading.Event(), threading.Event()
        def hold_state():
            with coordination.locked(self.path):
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(hold_state)
            try:
                self.assertTrue(entered.wait(5))
                started = time.monotonic()
                result = billing.reconcile(self.path, fetch=self.fail, timeout_seconds=.01)
                self.assertLess(time.monotonic() - started, .1)
                self.assertEqual(result, {'in_progress': True, 'waiting_for': 'run_state'})
                self.assertFalse((self.path.parent / 'billing-status.json').exists())
            finally:
                release.set()
            owner.result(timeout=5)
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['unmatched'], [])

    def test_state_contention_after_fetch_preserves_bill_and_cursor_for_next_read(self):
        entered, release = threading.Event(), threading.Event()
        def hold_state():
            with coordination.locked(self.path):
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=1) as pool:
            def fetch():
                pool.submit(hold_state)
                self.assertTrue(entered.wait(5))
                return {'recent': {'entries': [self.row], 'next_cursor': 'must-not-skip'}}
            try:
                result = billing.reconcile(self.path, fetch=fetch)
                self.assertEqual(result['waiting_for'], 'run_state')
                self.assertEqual(result['attempts'], 1)
                self.assertIsNone(result.get('next_cursor'))
                self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
            finally:
                release.set()
        final = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}}, refresh=True)
        self.assertEqual(final['attempts'], 2)
        self.assertEqual(final['unmatched'], [])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_contended_os_lock_is_not_unlocked_and_other_errors_stay_explicit(self):
        for error in (BlockingIOError('busy'), PermissionError('open denied')):
            with self.subTest(error=type(error).__name__):
                with patch.object(coordination, '_os_lock', side_effect=error), \
                     patch.object(coordination, '_unlock') as unlock:
                    with self.assertRaises(type(error)):
                        with coordination.locked(self.path, 'fixture-recovery', blocking=False):
                            self.fail('Lock was not acquired')
                    unlock.assert_not_called()
        # A failed acquisition must also release the local mutex/file descriptor.
        with coordination.locked(self.path, 'fixture-recovery', blocking=False):
            pass

    def test_settlement_keeps_state_owned_until_costs_and_status_are_saved(self):
        save_status = billing._save_status
        checked = []
        def can_write():
            try:
                with coordination.locked(self.path, blocking=False):
                    return True
            except BlockingIOError:
                return False
        with ThreadPoolExecutor(max_workers=1) as pool:
            def save(path, status):
                if status.get('matched') and not checked:
                    checked.append(True)
                    # A competing worker must not enter between the charge
                    # commit and its derived route/status updates.
                    self.assertFalse(pool.submit(can_write).result(timeout=5))
                    self.assertEqual(budget.read_object(self.path)['routes'][0]['cost_credits'], .5)
                return save_status(path, status)
            with patch.object(billing, '_save_status', side_effect=save):
                billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(checked, [True])
        self.assertEqual(budget.read_object(self.path.parent / 'billing-status.json')['unmatched'], [])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_windows_nonblocking_contention_uses_shared_busy_exception(self):
        fake = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, locking=Mock(side_effect=PermissionError('busy')))
        with patch.object(coordination.os, 'name', 'nt'), patch.object(coordination.os, 'lseek'), \
             patch.dict(sys.modules, {'msvcrt': fake}):
            with self.assertRaises(BlockingIOError):
                coordination._os_lock(7, blocking=False)
            with self.assertRaises(PermissionError):
                coordination._os_lock(7, blocking=True)
