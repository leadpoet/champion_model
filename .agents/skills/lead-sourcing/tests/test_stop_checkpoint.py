"""Terminal bookkeeping must survive an interrupted review without approving it."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_attempt_execution import runner
from test_stop_policy import action, stop_document


class StopCheckpointTests(unittest.TestCase):
    def test_terminal_checkpoint_preserves_work_errors_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            document = stop_document([action('next-source')], target_count=2, accepted=[{}], max_duration_seconds=1)
            document['stop_reason'] = 'continue'
            document['stop_audit'] = {'route_frontier': [
                {'route_id': 'next-source', 'state': 'untried', 'scope': 'discovery'}]}
            document['unresolved'] = [{'stage': 'account', 'candidate': {'domain': 'example.test'},
                                       'reason_text': 'Signal evidence incomplete'}]
            original = copy.deepcopy(document)
            path.write_text(json.dumps(document))
            with patch('run_attempt.confirmed_leads.update', side_effect=AssertionError('No approval')):
                audit = runner.save_stop_checkpoint(path)
                first = path.read_bytes()
                second = runner.save_stop_checkpoint(path)
            saved = json.loads(first)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(saved['stop_reason'], 'time_limit_reached')
            self.assertTrue(saved['stop_audit']['frontier_complete'])
            self.assertEqual(saved['stop_audit']['route_frontier'], original['stop_audit']['route_frontier'])
            for key in ('request', 'accepted', 'unresolved', 'routes', 'stop_check'):
                self.assertEqual(saved[key], original[key])
            self.assertFalse(audit['delivery_allowed'])
            self.assertFalse(second['delivery_allowed'])
            self.assertNotIn('final_review', saved)
            self.assertTrue(audit['errors'])  # The incomplete input has not been made deliverable.
            self.assertFalse(any('stop_reason must match' in e or 'frontier_complete must' in e for e in audit['errors']))

    def test_live_run_cannot_be_checkpointed_as_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            path.write_text(json.dumps(stop_document([action('research')], max_duration_seconds=None)))
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'terminal stop'):
                runner.save_stop_checkpoint(path)
            self.assertEqual(path.read_bytes(), before)
