"""Bound free startup recovery without replaying provider research."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_tools import FixtureProvider, setup_request
import research_tools
from research_tools import ResearchTools, OperationalBlock


class Clock:
    def __init__(self):
        self.elapsed = 0
        self.sleeps = []
        self.origin = datetime.now(timezone.utc)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def now(self, zone):
        return self.origin + timedelta(seconds=self.elapsed)


class StartupCatalogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'results.json'
        self.provider = FixtureProvider()
        self.clock = Clock()
        for name, value in [('time', self.clock), ('datetime', self.clock)]:
            patched = patch.object(research_tools, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.started = self.clock.origin.isoformat()

    def timeout(self, request, capture):
        capture({'timed_out': True, 'body': '', 'stderr': 'catalog connection stalled'})
        return {'provider': 'deepline', 'operation': 'describe', 'tool': request['tool'],
                'status': 'timeout', 'results': []}, 2

    def test_slow_free_catalog_recovers_on_longer_retry_with_saved_diagnostics(self):
        calls = []
        def execute(request, capture):
            calls.append(request)
            self.assertEqual(request['operation'], 'describe')
            if len(calls) == 1:
                self.clock.elapsed += request['timeout_seconds']
                return self.timeout(request, capture)
            self.clock.elapsed += 35  # Longer than the original 30-second limit.
            return self.provider(request, capture)
        self.tools.execute = execute
        result = self.tools._startup_contract('harvestapi_get_company', 'company-tool.json', self.started)
        self.assertEqual([r['timeout_seconds'] for r in calls], [30, 60])
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['started_at'], self.started)
        self.assertEqual(result['catalog_attempt']['number'], 2)
        self.assertEqual(result['catalog_attempt']['elapsed_seconds'], 35)
        old = json.loads(next(self.path.parent.glob('company-tool-*.json')).read_text())
        self.assertEqual(old['catalog_attempt']['number'], 1)
        self.assertEqual(old['catalog_attempt']['elapsed_seconds'], 30)
        self.assertEqual(old['provider_response']['stderr'], 'catalog connection stalled')
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name('results.json.budget.json').exists())
        self.tools.execute = lambda *args: self.fail('Successful metadata must be reused')
        self.assertEqual(self.tools._startup_contract('harvestapi_get_company', 'company-tool.json', self.started), result)

    def test_shared_startup_window_includes_backoff(self):
        calls = []
        def execute(request, capture):
            calls.append(request)
            self.clock.elapsed += request['timeout_seconds']
            return self.timeout(request, capture)
        self.tools.execute = execute
        # Another prerequisite consumed most of the shared 120-second window.
        self.clock.elapsed = 89
        with self.assertRaisesRegex(OperationalBlock, 'deadline reached'):
            self.tools._startup_contract('harvestapi_get_company', 'company-tool.json', self.started, until=120)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['timeout_seconds'], 30)
        self.assertEqual(self.clock.elapsed, 120)
        self.assertEqual(self.clock.sleeps, [1])
        self.assertEqual(json.loads((self.path.parent/'company-tool.json').read_text())['status'], 'timeout')

    def test_run_deadline_shortens_retry_and_rejects_late_success(self):
        calls = []
        def execute(request, capture):
            calls.append(request)
            self.clock.elapsed += request['timeout_seconds']
            return self.timeout(request, capture) if len(calls) == 1 else self.provider(request, capture)
        self.tools.execute = execute
        with self.assertRaisesRegex(OperationalBlock, 'deadline reached'):
            self.tools._startup_contract('harvestapi_get_company', 'company-tool.json', self.started,
                                         max_duration_seconds=40)
        self.assertEqual([r['timeout_seconds'] for r in calls], [30, 8])
        self.assertEqual(self.clock.elapsed, 40)
        receipt = json.loads((self.path.parent/'company-tool.json').read_text())
        self.assertEqual(receipt['status'], 'ok')  # Preserve the response, never reset the clock.
        self.assertEqual(receipt['started_at'], self.started)
        self.assertFalse(self.path.exists())

    def test_retry_of_saved_failure_preserves_expired_original_clock(self):
        old_start = (self.clock.origin - timedelta(seconds=100)).isoformat()
        receipt = self.path.parent/'company-tool.json'
        receipt.write_text(json.dumps({'started_at': old_start, 'status': 'timeout', 'results': []}))
        before = receipt.read_bytes()
        self.tools.execute = lambda *args: self.fail('Expired setup cannot dispatch')
        with self.assertRaisesRegex(OperationalBlock, 'deadline reached'):
            self.tools._startup_contract('harvestapi_get_company', 'company-tool.json', self.started,
                                         max_duration_seconds=60)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob('company-tool-*.json')))

    def test_native_start_passes_original_user_deadline_to_every_prerequisite(self):
        request = setup_request()['request']
        request['max_duration_seconds'] = 20
        request['contact_fields'] = ['email']
        old_start = (self.clock.origin - timedelta(seconds=30)).isoformat()
        self.tools.execute = lambda *args: self.fail('Expired setup cannot dispatch')
        with patch.dict('os.environ', {'TYCHE_RUN_STARTED_AT': old_start}):
            result = self.tools.call('tyche_start', {'request': request})
        self.assertEqual(result['status'], 'operationally_blocked')
        self.assertIn('deadline reached', result['reason'])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name('results.json.budget.json').exists())


if __name__ == '__main__':
    unittest.main()
