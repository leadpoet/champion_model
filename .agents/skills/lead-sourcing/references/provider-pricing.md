# Provider costs and the run cutoff

New runs use `budget.policy: actual_cost` and a version 2 execution ledger.
The local launcher counts provider charges plus estimated base LLM
cost from individual response usage, including retries and compaction. The
budget is a stopping threshold. A call already in flight can cross it; no new
paid work starts after the threshold is observed. No money is reserved for
future calls or email verification, and the model supplies no price guesses.

Historical reserved-budget runs retain catalog-backed and versioned managed-price reservations. Their original ledgers, verification reserve and hard provider caps are preserved.

Use live catalog pricing to choose suitable tools. Actual billing settles each
saved Deepline request. New Deepline responses must explicitly report
`pricing_status: final`; `settlement_status: queued` means posting is pending,
not that the price is unknown. Estimated or missing price finality stays pending.
Previously saved legacy receipts retain their original interpretation.
A failed or empty result is not proof of a free call. Missing
billing pauses further paid work, without inventing an upper bound. A final
posted zero charge can settle a call as free. For version 2 runs, a successfully
completed call can also settle at zero when its saved pre-call provider contract
explicitly sets an unconditional zero per-call credit price. Preserve hashes of
that contract and response as `free_evidence`; report its route under
`catalog_free_calls`. This is contract evidence, not a billing receipt. Variable,
conditional, failed or incompletely captured calls still require billing.
An exact matching billing record with `status: error`, `charge_state: failed`,
`reason: operation_attempt` and explicit zero credits/delta can settle a failed
response with no returned results. The error response alone cannot settle it.
Preserve IDs, receipts and the
original limits; never repeat a paid call to discover its cost.

`run-costs.json` shows `provider_usd`, `estimated_llm_usd`, `total_usd` and
`pending_provider_calls`. The total is the known subtotal when status is
`incomplete`. Base LLM estimates exclude Fast premiums, hosted tools and
subscription allocation; they are not an invoice. Arena owns its own model
usage and combined cutoff; no local model charge is fabricated there.

Local billing reconciliation uses the execution API credentials. It reads the
credit ledger for individual `charge_settle` debits, then the usage feed for
explicit free/failed outcomes. Grouped usage totals are never divided among calls
or added to individual debits. Rounded display fields such as `rough_usd_cost`
are never accounting inputs. Reported credits and exact USD remain separate;
the configured credit conversion is used only when exact USD is absent.

Reads match exact request IDs and provider operations, with up to four 100-row
pages per feed within one shared 30-second deadline. CLI-only credentials retain
the bounded usage fallback. Automatic reads retain a three-attempt
allowance and cooldown. Each validated page settles attributable charges before
saving its continuation cursor. A later-page failure preserves those charges;
the next API read checks the newest page for late postings before continuing
older history. The usage API uses its documented numeric offsets: its opaque
cursor repeated pages during live validation. Offsets must advance, repeated
page identities are rejected, and identical cross-page overlaps are not counted
twice. A ledger transport outage can use a healthy usage feed; malformed records
or organization changes fail closed. `billing-status.json`
records each attempt's page numbers, elapsed seconds, failure categories and
unmatched calls without copying provider output. After an outage, explicitly
resume billing-only reads:

```bash
python3 .agents/skills/lead-sourcing/scripts/billing_reconciliation.py reports/<run-id>/results.json --resume
```

This grants three further read attempts and preserves their history. It never
dispatches research, clears missing charges, raises a limit or resets spend.
Arena remains credential-free: its broker must supply authoritative billing
in the operation response. It does not gain direct HTTP access to these ledgers.
Missing request IDs still require authoritative provider evidence. Transport
receipts retain the failure stage, exception type, elapsed time and numeric
errno without exception messages or credentials. They do not prove a call was
unsent. ScrapingDog uses versioned documented endpoint tariffs for completed requests,
including empty results. Explicit provider failures cost zero under its published
policy. Variable tariffs and unresolved responses retain the documented maximum
as `held_credits`, separate from actual charges. The ledger reserves that ceiling
before dispatch and counts it against both limits, so concurrent calls cannot
reuse it. This allows a different request to continue without replaying the
uncertain call. Unverified option combinations still pause for billing evidence.

`held_provider_usd` and `budget_total_usd` appear when tariff holds remain; the
latter includes charges, holds and model estimates. The known subtotal never
includes a hold as billed spend. Sources, version, original request and raw
response are checked by the strict ledger audit. ScrapingDog stays disabled by
default and requires a saved plan conversion; account-wide balance differences
must not be attributed to one concurrent run. See [its adapter](scrapingdog-adapter.md).

Spending comparisons retain decimal precision until the JSON report boundary.
Each guarded dispatch holds an OS lease for its lifetime. Concurrent live calls
remain permitted; after a crash, an unowned `in_flight` reservation is counted as
pending billing and prevents further spending. The reservation and original
receipt remain intact. Ledger writes reuse crash-released OS locks; unidentified
legacy lock files still require owner verification rather than time-based expiry.

## Historical runs

Version 1 ledgers retain their original reservation checks and measured-price
fallbacks. They are not silently migrated. Their original caps, holds and
price-overrun blocks remain auditable. `provider_pricing.py` supports these
historical paths; its observed rates are not provider guarantees. After an
actual pricing repair, `budget_guard.py --reconcile-receipt` can validate a
historical overrun receipt without changing its original limits or charges.
