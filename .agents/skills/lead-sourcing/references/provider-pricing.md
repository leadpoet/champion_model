# Provider costs and the run cutoff

New runs use `budget.policy: actual_cost` and a version 2 execution ledger.
The local launcher counts reported provider charges plus estimated base LLM
cost from individual response usage, including retries and compaction. The
budget is a stopping threshold. A call already in flight can cross it; no new
paid work starts after the threshold is observed. No money is reserved for
future calls or email verification, and the model supplies no price guesses.

Use live catalog pricing to choose suitable tools. Actual billing settles each
saved request. A failed or empty result is not proof of a free call. Missing
billing pauses further paid work, without inventing an upper bound. A final
posted zero charge can settle a call as free. For version 2 runs, a successfully
completed call can also settle at zero when its saved pre-call provider contract
explicitly sets an unconditional zero per-call credit price. Preserve hashes of
that contract and response as `free_evidence`; report its route under
`catalog_free_calls`. This is contract evidence, not a billing receipt. Variable,
conditional, failed or incompletely captured calls still require billing.
Preserve IDs, receipts and the
original limits; never repeat a paid call to discover its cost.

`run-costs.json` shows `provider_usd`, `estimated_llm_usd`, `total_usd` and
`pending_provider_calls`. The total is the known subtotal when status is
`incomplete`. Base LLM estimates exclude Fast premiums, hosted tools and
subscription allocation; they are not an invoice. Arena owns its own model
usage and combined cutoff; no local model charge is fabricated there.

Billing reconciliation matches exact request IDs and provider operations, with
up to four 50-row pages per bounded read. Automatic reads retain a three-attempt
allowance and cooldown. After an outage, explicitly resume billing-only reads:

```bash
python3 .agents/skills/lead-sourcing/scripts/billing_reconciliation.py reports/<run-id>/results.json --resume
```

This grants three further read attempts and preserves their history. It never
dispatches research, clears missing charges, raises a limit or resets spend.
Missing request IDs still require authoritative provider evidence. The current
ScrapingDog wrapper has no per-request billing receipt, so enabling it leaves
calls pending under this policy. It stays disabled by default; account-wide
balance differences must not be attributed to one concurrent run.

## Historical runs

Version 1 ledgers retain their original reservation checks and measured-price
fallbacks. They are not silently migrated. Their original caps, holds and
price-overrun blocks remain auditable. `provider_pricing.py` supports these
historical paths; its observed rates are not provider guarantees. After an
actual pricing repair, `budget_guard.py --reconcile-receipt` can validate a
historical overrun receipt without changing its original limits or charges.
