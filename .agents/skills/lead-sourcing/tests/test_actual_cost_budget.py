"""Observed spend, including model usage, is a cutoff rather than a forecast."""
import json
from pathlib import Path
import tempfile
import unittest

from test_provider_scripts import ROOT
import budget_guard as budget


class ActualCostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "results.json"
        self.path.write_text(json.dumps({"request": {"target_count": 5, "contact_fields": ["email"]},
            "accepted": [], "routes": [], "budget": {"policy": "actual_cost", "paid_calls": 0,
                "limits": {"deepline_credits": 25, "scrapingdog_credits": 0}}}))
        budget.initialize(self.path, max_usd=2.5)

    def call(self, rid, billing=None):
        return budget.guarded_call({"spend": {"run_file": str(self.path), "route_id": rid}}, "deepline",
            lambda: ({"status": "ok", **({"billing": billing} if billing is not None else {})}, 0))

    def model(self, value, *, reconciled=True, rid="response-1"):
        folder = self.root / "model-usage"
        folder.mkdir(exist_ok=True)
        (folder / "worker.json").write_text(json.dumps({"request_file": str(self.root / "request.txt"),
            "finished_at": "2026-09-18", "usage_reconciled": reconciled,
            "responses": [{"response_id": rid, "model": "fixture", "usage": {}, "estimated_base_usd": value}]}))

    def test_unpriced_call_can_cross_combined_cap_but_next_call_never_dispatches(self):
        self.model(2.3)
        body, code = self.call("one", {"credits_charged": 3, "cost_usd": .3})
        self.assertEqual(code, 0)
        self.assertEqual(body["spend_receipt"]["state"], "settled")
        state = budget.load_ledger(self.path)
        self.assertNotIn("maximum_credits", state["calls"]["one"])
        self.assertEqual(budget.actual_cost_summary(state)["total_usd"], 2.6)
        body, code = self.call("two", {"credits_charged": 1})
        self.assertEqual(code, 2)
        self.assertFalse(body["request_sent"])
        self.assertIn("budget_exhausted", body["error"]["message"])
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 1)

    def test_unknown_bill_is_preserved_while_distinct_calls_use_confirmed_cost(self):
        self.call("unknown")
        state = budget.load_ledger(self.path)
        summary = budget.actual_cost_summary(state)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["pending_provider_calls"], 1)
        self.assertEqual(summary["total_usd"], 0)
        self.assertNotIn("maximum", json.dumps(summary))
        self.assertEqual(budget.spending_stop(state), "billing_pending")
        self.assertIsNone(budget.admission_stop(state))
        next_body, next_code = self.call("next")
        self.assertEqual(next_code, 0)
        self.assertEqual(next_body["spend_receipt"]["state"], "pending_billing")
        with self.assertRaisesRegex(budget.BudgetError, "already"):
            budget.reserve({"run_file": str(self.path), "route_id": "unknown"}, "deepline")
        budget.settle(budget.ledger_path(self.path), "unknown", {"cost_usd": 3})
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.admission_stop(state), "budget_exhausted")
        blocked, code = self.call("after-confirmed-cap")
        self.assertEqual(code, 2)
        self.assertFalse(blocked["request_sent"])
        self.assertEqual(set(state["calls"]), {"unknown", "next"})

    def test_in_flight_calls_have_identity_without_monetary_holds(self):
        for rid in ("one", "two"):
            budget.reserve({"run_file": str(self.path), "route_id": rid}, "deepline")
        state = budget.load_ledger(self.path)
        self.assertIsNone(budget.spending_stop(state))
        self.assertEqual(budget.actual_cost_summary(state)["pending_provider_calls"], 2)
        with self.assertRaisesRegex(budget.BudgetError, "already"):
            budget.reserve({"run_file": str(self.path), "route_id": "one"}, "deepline")

    def test_llm_alone_stops_at_exact_threshold_and_receipts_survive_restart(self):
        self.model(2.5)
        self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), "budget_exhausted")
        with self.assertRaisesRegex(budget.BudgetError, "already exists"):
            budget.initialize(self.path, max_usd=20)
        self.assertEqual(budget.load_ledger(self.path)["usd_limit"], "2.5")

    def test_incomplete_model_usage_blocks_continuation_but_keeps_known_cost(self):
        self.model(.4, reconciled=False)
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.actual_cost_summary(state)["total_usd"], .4)
        self.assertEqual(budget.spending_stop(state), "model_usage_pending")
        self.assertIsNone(budget.admission_stop(state))

    def test_confirmed_failed_call_cost_still_closes_admission(self):
        body, code = budget.guarded_call(
            {"spend": {"run_file": str(self.path), "route_id": "failed"}},
            "deepline",
            lambda: ({"status": "provider_error", "billing": {"cost_usd": 2.5}}, 2),
        )
        self.assertEqual(code, 2)
        self.assertEqual(body["spend_receipt"]["state"], "settled")
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.actual_cost_summary(state)["provider_usd"], 2.5)
        self.assertEqual(budget.admission_stop(state), "budget_exhausted")

    def test_duplicate_response_is_counted_once_and_conflicts_are_rejected(self):
        self.model(.4)
        folder = self.root / "model-usage"
        data = json.loads((folder / "worker.json").read_text())
        (folder / "second.json").write_text(json.dumps(data))
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["total_usd"], .4)
        data["responses"][0]["estimated_base_usd"] = .8
        (folder / "second.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(budget.BudgetError, "conflicting"):
            budget.actual_cost_summary(budget.load_ledger(self.path))

    def test_reported_usd_takes_precedence_over_credit_conversion(self):
        self.call("one", {"credits_charged": 3, "cost_usd": .24})
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["provider_usd"], .24)

    def test_only_current_worker_may_have_unfinished_usage(self):
        self.model(.4)
        path = self.root / "model-usage/worker.json"
        receipt = json.loads(path.read_text())
        receipt.update(finished_at=None, usage_reconciled=False)
        path.write_text(json.dumps(receipt))
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.spending_stop(state), "model_usage_pending")
        self.assertIsNone(budget.spending_stop(state, active_model_receipt="worker"))
        self.assertEqual(budget.actual_cost_summary(state, "worker")["status"], "incomplete")
        (path.parent / "abandoned.json").write_text(json.dumps(receipt))
        self.assertEqual(budget.spending_stop(state, active_model_receipt="worker"), "model_usage_pending")

    def test_usd_only_billing_is_settled_once_and_survives_resume(self):
        self.call("one", {"cost_usd": .24})
        state = budget.load_ledger(self.path)
        self.assertIsNone(budget.spending_stop(state))
        self.assertEqual(budget.actual_cost_summary(state)["pending_provider_calls"], 0)
        before = budget.ledger_path(self.path).read_bytes()
        with self.assertRaises(budget.BudgetError):
            budget.settle(budget.ledger_path(self.path), "one", {"credits_charged": 7})
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_credit_caps_use_reported_credits_even_when_usd_is_discounted(self):
        self.call("one", {"credits_charged": 25, "cost_usd": 1})
        self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), "budget_exhausted")

    def test_usd_only_bill_counts_toward_explicit_next_lead_threshold(self):
        self.call("one", {"cost_usd": .5})
        state = budget.load_ledger(self.path)
        state["next_lead_limit"] = "5"
        self.assertEqual(budget.spending_stop(state, accepted_count=0), "budget_exhausted")
        self.assertIsNone(budget.spending_stop(state, accepted_count=1))
