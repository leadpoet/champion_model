"""Cross-process ownership/accounting tests; no live providers or model calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard as budget
import run_coordination as coordination
from record_route import mutate
from research_tools import ResearchTools
from test_research_tools import FixtureProvider, check
from test_research_interface import setup_request


def contest(path, worker, ready, start, output):
    ready.put(worker)
    start.wait(60)
    output.put(coordination.claim(path, worker, worker, "https://www.example.test/about"))


def reserve_money(path, index, ready, start, output):
    ready.put(index)
    start.wait(60)
    try:
        budget.reserve({"run_file": path, "route_id": "call-" + str(index), "max_cost_credits": 4}, "deepline")
        output.put(True)
    except budget.BudgetError:
        output.put(False)


def write_rows(path, start, count):
    for index in range(count):
        def add(document):
            document["values"].append(start + index)
            return document
        mutate(path, add)


def hold_slot(path, ready, release):
    with coordination.provider_slot(path):
        ready.put(True)
        release.wait(60)


class ParallelWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "results.json"
        self.context = multiprocessing.get_context("spawn")

    def configure(self):
        coordination.configure(self.path, 3)
        for i in range(1, 4):
            worker = f"worker-{i}"
            coordination.register(self.path, worker, worker)

    def worker(self, number, provider=None):
        worker = f"worker-{number}"
        return ResearchTools(self.path, execute=provider or FixtureProvider(), environment={
            "TYCHE_WORKER_ID": worker, "TYCHE_WORKER_GENERATION": worker})

    def start_run(self, cap=5):
        request = copy.deepcopy(setup_request()["request"])
        request["contact_fields"] = []
        ResearchTools(self.path, execute=FixtureProvider()).start(request, max_usd=cap)

    def test_three_processes_claim_same_company_only_one_wins(self):
        self.configure()
        ready, output = self.context.Queue(), self.context.Queue()
        start = self.context.Event()
        jobs = [self.context.Process(target=contest, args=(str(self.path), f"worker-{i}", ready, start, output)) for i in range(1, 4)]
        for process in jobs:
            process.start()
        for _ in jobs:
            ready.get(timeout=60)
        start.set()
        results = [output.get(timeout=60) for _ in jobs]
        for process in jobs:
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        self.assertEqual(len(coordination.snapshot(self.path)["claims"]), 1)

    def test_known_aliases_share_owner_and_stale_invocation_cannot_write(self):
        self.configure()
        first = coordination.claim(self.path, "worker-1", "worker-1", "example.test", ["https://linkedin.com/company/example/"])
        self.assertTrue(first["claimed"])
        self.assertFalse(coordination.claim(self.path, "worker-2", "worker-2", "https://www.linkedin.com/company/EXAMPLE/")["claimed"])
        coordination.register(self.path, "worker-1", "replacement")
        with self.assertRaisesRegex(ValueError, "no longer owns"):
            coordination.require_claim(self.path, "worker-1", "worker-1", "example.test")
        coordination.require_claim(self.path, "worker-1", "replacement", "example.test")
        self.assertEqual(coordination.snapshot(self.path)["claims"]["example.test"]["worker"], "worker-1")

    def test_cross_process_budget_reservations_cannot_overspend(self):
        self.start_run(cap=.5)
        ready, output = self.context.Queue(), self.context.Queue()
        start = self.context.Event()
        jobs = [self.context.Process(target=reserve_money, args=(str(self.path), i, ready, start, output)) for i in range(3)]
        for process in jobs:
            process.start()
        for _ in jobs:
            ready.get(timeout=60)
        start.set()
        results = [output.get(timeout=60) for _ in jobs]
        for process in jobs:
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 1)

    def test_simultaneous_writes_preserve_all_rows(self):
        self.path.write_text(json.dumps({"values": []}))
        jobs = [self.context.Process(target=write_rows, args=(str(self.path), i * 20, 20)) for i in range(3)]
        for process in jobs:
            process.start()
        for process in jobs:
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(json.loads(self.path.read_text())["values"]), list(range(60)))

    def test_provider_limit_is_global_across_processes(self):
        ready, release = self.context.Queue(), self.context.Event()
        jobs = [self.context.Process(target=hold_slot, args=(str(self.path), ready, release)) for _ in range(4)]
        for process in jobs:
            process.start()
        try:
            for _ in range(3):
                ready.get(timeout=60)
            from queue import Empty
            with self.assertRaises(Empty):
                ready.get(timeout=.3)
        finally:
            release.set()
            for process in jobs:
                process.join(60)
                self.assertEqual(process.exitcode, 0)

    def test_different_companies_really_dispatch_concurrently_and_other_owner_is_refused(self):
        self.start_run()
        self.configure()
        provider = FixtureProvider()
        provider.delay = .2
        workers = [self.worker(i, provider) for i in range(1, 4)]
        for i, worker in enumerate(workers):
            worker.claim(f"company-{i}.test")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(worker.lookup, [check(f"company-{i}.test")]) for i, worker in enumerate(workers)]
            for future in futures:
                self.assertEqual(future.result()["lookups"][0]["status"], "ok")
        self.assertEqual(provider.peak, 3)
        before = len(provider.requests)
        with self.assertRaisesRegex(ValueError, "owned by another"):
            workers[1].lookup([check("company-0.test")])
        self.assertEqual(len(provider.requests), before)

    def test_one_worker_cannot_finalize_while_others_research(self):
        self.start_run()
        self.configure()
        result = self.worker(1).finish(review_ref="pretend-review")
        self.assertEqual(result["status"], "needs_research")
        self.assertFalse(result["delivery_allowed"])
        self.assertFalse((self.path.parent / "leads.xlsx").exists())

    def test_reconfigure_cannot_reset_claims_or_change_pool_size(self):
        self.configure()
        self.worker(1).claim("example.test")
        coordination.configure(self.path, 3)
        self.assertFalse(self.worker(2).claim("example.test")["claimed"])
        with self.assertRaisesRegex(ValueError, "original worker count"):
            coordination.configure(self.path, 1)

    def test_linkedin_identity_resolves_to_existing_domain_or_requires_discovery(self):
        self.configure()
        worker = self.worker(1)
        self.assertEqual(worker.claim("https://linkedin.com/company/example/")["status"], "domain_required")
        self.assertTrue(worker.claim("example.test", "https://linkedin.com/company/example/")["claimed"])
        self.assertEqual(worker.claim("https://www.linkedin.com/company/example/")["target"], "example.test")

    def test_discovery_label_cannot_bypass_company_or_email_ownership(self):
        self.start_run()
        self.configure()
        provider = FixtureProvider()
        worker = self.worker(1, provider)
        for tool, inputs in [("harvestapi_get_company", {"url": "https://linkedin.com/company/example"}),
                             ("harvestapi_get_profile", {"url": "https://linkedin.com/in/example"}),
                             ("zerobounce_validate", {"email": "person@example.test"}),
                             ("custom_company_reader", {"filters": {"domain": "example.test"}})]:
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                worker.call("tyche_lookup", {"checks": [check("discovery", tool=tool, inputs=inputs, phase="account_discovery")]})
        self.assertEqual(provider.requests, [])

    def test_known_aliases_cannot_enter_same_batch_twice(self):
        self.start_run()
        self.configure()
        provider = FixtureProvider()
        worker = self.worker(1, provider)
        url = "https://linkedin.com/company/example/"
        worker.claim("example.test", url)
        with self.assertRaisesRegex(ValueError, "distinct canonical company scopes"):
            worker.lookup([check("example.test", inputs={"url": url}), check(url, inputs={"url": url})])
        self.assertFalse(any(request["operation"] == "execute" for request in provider.requests))

    def test_replaced_worker_saves_receipt_but_cannot_write_results(self):
        self.start_run()
        self.configure()
        provider = FixtureProvider()
        def replace_after_response(request, capture):
            result = provider(request, capture)
            if request["operation"] == "execute":
                coordination.register(self.path, "worker-1", "replacement")
            return result
        worker = self.worker(1, replace_after_response)
        worker.claim("example.test")
        with self.assertRaisesRegex(ValueError, "no longer owns"):
            worker.call("tyche_lookup", {"checks": [check()]})
        ledger = budget.load_ledger(self.path)
        self.assertEqual(len(ledger["calls"]), 1)
        route = next(iter(ledger["calls"]))
        self.assertFalse(any(row["route_id"] == route for row in json.loads(self.path.read_text())["routes"]))
        import run_attempt
        recovered = run_attempt.recover_completed_attempts(self.path)
        self.assertEqual(recovered["recovered"], [route])
        self.assertEqual(len(ledger["calls"]), 1)

    def test_changed_accepted_count_refuses_unsent_reservation_without_spending(self):
        self.start_run()
        spend = {"run_file": str(self.path), "route_id": "race", "max_cost_credits": .2, "accepted_before": 0}
        def review(document):
            document["accepted"].append({"company": {"domain": "accepted.test"}})
            return document
        mutate(self.path, review)
        with self.assertRaisesRegex(budget.BudgetError, "no request sent"):
            budget.reserve(spend, "deepline")
        self.assertEqual(budget.load_ledger(self.path)["calls"], {})
        spend["accepted_before"] = 1
        budget.reserve(spend, "deepline")
        self.assertEqual(budget.load_ledger(self.path)["calls"]["race"]["accepted_leads_before_call"], 1)

    def test_progress_race_can_replan_same_proven_unsent_lookup(self):
        self.start_run()
        self.configure()
        provider = FixtureProvider()
        raced = False
        def progress_race(request, capture):
            nonlocal raced
            if request["operation"] != "execute" or raced:
                return provider(request, capture)
            raced = True
            def add(document):
                document["accepted"].append({"company": {"domain": "accepted.test"}})
                return document
            def remove(document):
                document["accepted"].pop()
                return document
            mutate(self.path, add)
            try:
                return provider(request, capture)
            finally:
                mutate(self.path, remove)
        worker = self.worker(1, progress_race)
        worker.claim("example.test")
        refused = worker.call("tyche_lookup", {"checks": [check()]})
        self.assertEqual(refused["lookups"][0]["status"], "config_error")
        self.assertIsNone(worker._operational_block())
        self.assertEqual(budget.load_ledger(self.path)["calls"], {})
        retried = worker.call("tyche_lookup", {"checks": [check()]})
        self.assertEqual(retried["lookups"][0]["status"], "ok")
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 1)

    def test_foreign_coordination_state_cannot_authorize_claims(self):
        self.configure()
        foreign = self.path.with_name("foreign.json")
        coordination.state_path(foreign).write_bytes(coordination.state_path(self.path).read_bytes())
        with self.assertRaisesRegex(ValueError, "different run"):
            coordination.snapshot(foreign)
        with self.assertRaisesRegex(ValueError, "different run"):
            coordination.claim(foreign, "worker-1", "worker-1", "example.test")

    def test_missing_and_malformed_coordination_fail_closed_without_reset(self):
        with self.assertRaisesRegex(ValueError, "coordination state"):
            coordination.check_worker(None, "worker-1", "worker-1")
        self.configure()
        path = coordination.state_path(self.path)
        for broken in ({}, {"version": 1, "run_file": str(self.path.resolve()), "worker_count": 3}):
            path.write_text(json.dumps(broken))
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "coordination state"):
                coordination.configure(self.path, 3)
            self.assertEqual(path.read_bytes(), before)

    def test_terminal_company_can_receive_own_alias_patch_but_not_another_owner(self):
        self.configure()
        self.worker(1).claim("example.test")
        coordination.reviewed(self.path, "worker-1", "worker-1", "example.test", "accept")
        coordination.require_claim(self.path, "worker-1", "worker-1", "example.test", ["https://linkedin.com/company/example/"])
        self.assertFalse(self.worker(2).claim("https://linkedin.com/company/example/")["claimed"])


if __name__ == "__main__":
    unittest.main()
