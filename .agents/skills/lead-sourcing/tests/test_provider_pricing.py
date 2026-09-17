"""Managed fallback pricing must fail before provider dispatch when uncertain."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deepline
import provider_pricing as pricing


class ManagedPricingTests(unittest.TestCase):
    def setUp(self):
        self.contract = {"toolId": "harvestapi_get_profile", "billingSource": "managed_by_deepline",
                         "pricing": {"unit": "usage", "creditsPerUnit": None}}
        self.inputs = {"url": "https://www.linkedin.com/in/example", "main": "true"}
        self.catalog = json.loads(pricing.MANAGED_PRICES.read_text())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "prices.json"
        self.save_catalog()
        config = patch.object(pricing, "MANAGED_PRICES", self.path)
        config.start()
        self.addCleanup(config.stop)
        clock = patch.object(pricing, "datetime")
        clock.start().now.return_value = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)

    def save_catalog(self):
        self.path.write_text(json.dumps(self.catalog))

    def request(self):
        return {"operation": "execute", "tool": self.contract["toolId"], "payload": copy.deepcopy(self.inputs),
                "spend": {"run_file": "unused.json", "route_id": "profile-1", "max_cost_credits": .03,
                          "pricing_basis": pricing.profile_price(self.contract, self.inputs)}}

    def assert_refused(self, request):
        with patch.object(deepline, "guarded_call") as dispatch:
            body, code = deepline.run(request)
        self.assertEqual(code, 2)
        self.assertEqual(body["error_stage"], "pricing")
        self.assertFalse(body["request_sent"])
        dispatch.assert_not_called()

    def test_managed_scope_required_even_with_override(self):
        for scope in (None, "bring_your_own_key", "direct", "unknown"):
            with self.subTest(scope=scope):
                contract = {**self.contract, "billingSource": scope}
                self.assertEqual(pricing.stored_profile_prices(contract), [])
                with self.assertRaisesRegex(ValueError, "No whole-call price"):
                    pricing.call_credits(contract, self.inputs, 1)

    def test_expiry_and_future_dates_block_before_dispatch(self):
        request = self.request()
        for key, value in (("valid_until", "2026-09-17"), ("verified_at", "2026-09-18")):
            with self.subTest(key=key):
                original = self.catalog[key]
                self.catalog[key] = value
                self.save_catalog()
                self.assert_refused(request)
                self.catalog[key] = original

    def test_catalog_price_including_explicit_zero_overrides_stale_fallback(self):
        self.catalog["valid_until"] = "2026-09-16"
        self.save_catalog()
        for value in (0, .07):
            contract = {**self.contract, "pricing": {"unit": "call", "creditsPerUnit": value}}
            self.assertEqual(pricing.call_credits(contract, self.inputs), value)
            self.assertEqual(pricing.stored_profile_prices(contract), [])

    def test_changed_identity_options_price_and_version_invalidate_reservation(self):
        original = self.request()
        for payload in ({**self.inputs, "url": "https://www.linkedin.com/in/other"},
                        {**self.inputs, "findEmail": "true"}, {**self.inputs, "main": True}):
            with self.subTest(payload=payload):
                self.assert_refused({**original, "payload": payload})
        for key, value in (("version", "new-reviewed-version"), ("verified_at", "2026-09-15")):
            with self.subTest(key=key):
                old = self.catalog[key]
                self.catalog[key] = value
                self.save_catalog()
                self.assert_refused(original)
                self.catalog[key] = old
        self.catalog["prices"][0]["credits"] = .04
        self.save_catalog()
        self.assert_refused(original)

    def test_under_reservation_refused_and_valid_record_reaches_existing_guard(self):
        request = self.request()
        request["spend"]["max_cost_credits"] = .02
        self.assert_refused(request)
        request["spend"]["max_cost_credits"] = .03
        with patch.object(deepline, "guarded_call", return_value=({"status": "ok"}, 0)) as dispatch:
            self.assertEqual(deepline.run(request)[1], 0)
        dispatch.assert_called_once()

    def test_invalid_duplicate_missing_evidence_and_nonfinite_rates_fail_closed(self):
        original = copy.deepcopy(self.catalog)
        mutations = [lambda c: c["prices"].append(copy.deepcopy(c["prices"][0])),
                     lambda c: c["prices"][0].update(credits=-1),
                     lambda c: c["prices"][0].update(credits=True),
                     lambda c: c["prices"][0].update(credits=float("nan")),
                     lambda c: c["prices"][0].update(receipt_sha256=""),
                     lambda c: c.update(schema_version=True),
                     lambda c: c["prices"][0].update(inputs={"url": "one person"})]
        for mutate in mutations:
            self.catalog = copy.deepcopy(original)
            mutate(self.catalog)
            self.save_catalog()
            with self.assertRaises(ValueError):
                pricing.call_credits(self.contract, self.inputs)

    def test_fresh_configuration_is_loaded_without_process_restart(self):
        self.assertEqual(pricing.call_credits(self.contract, self.inputs), .03)
        self.catalog["prices"][0]["credits"] = .04
        self.catalog["version"] = "new-reviewed-version"
        self.save_catalog()
        self.assertEqual(pricing.call_credits(self.contract, self.inputs), .04)
        self.assertEqual(pricing.profile_price(self.contract, self.inputs)["catalog_version"], "new-reviewed-version")


if __name__ == "__main__":
    unittest.main()
