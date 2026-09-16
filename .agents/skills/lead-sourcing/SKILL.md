---
name: lead-sourcing
description: Source evidence-backed companies with buying signals and requested-role contacts using local Deepline/ScrapingDog wrappers; for company-first lists, not contact-only enrichment or outreach.
---

# TYCHE Lead Sourcing

LLM researches/qualifies; tools handle bookkeeping/validation. No CRM writes or outreach.

## Start or resume

Resume with `tyche_inspect()`. Preserve the saved request, authorization, budget and
pending work; reuse sources, descriptions and verified contacts/emails. Correct
only affected fields. Read references for unfamiliar actions or specific errors,
not routine bookkeeping.

For fresh runs, read [workflow rules](references/workflow-rules.md),
[input contract](references/output-contract.md#input-contract) and
[lifecycle invariants](references/output-contract.md#lifecycle-invariants).
Interpret once against launcher-saved `original_text`; only users change criteria.
Do not strengthen, weaken or add requirements. Company geography does not restrict
contact/activity location unless requested; hiring signals do not restrict buyer roles.
`product_service.perspective` distinguishes the user's `seller` offering from the
`target` company's offering; never invent a seller.
Save signals as required/preferred; put non-signal must-haves in `icp.required_attributes`.
Start within authorization using `tyche_start`; code supplies time, IDs, ledger,
reserve and runtime paths. Defaults: one contact/company, USD 0.50/requested lead.
Omit unrequested age limits and `max_duration_seconds`; speed benchmarks are not deadlines.
Catalog prices override [planning rates](references/provider-pricing.md); receipts
supply charges. Never import runs or guess prices.
Use [native tools](references/adapter-io.md#native-tools), not shell bookkeeping or implementation-code reads.

## Research loop

1. **Choose ready work.** Prefer affordable, unblocked `completion_candidates` before discovery.
   Read [tools.md](references/tools.md#choose-by-evidence-gap) once; select capabilities
   for the next evidence gap. Reuse `cached_descriptions`; discover alternatives with
   `tyche_inspect(query=...)`. Learn a selected `tool` once; inspect `field` for detail.
   Code checks contracts/prices; refresh only after schema/price/access changes.
   Pilot unproven operations/filters before batching; preserve native limits.
2. **Check up to three companies concurrently.** Batch independent checks across phases
   without waiting for full batches. Review fit/signals before buyers. Search snippets
   identify candidates; read the source body once for required web claims, reusing saved text.
   Preserve announced, conditional, planned and completed status; distinguish `event_date`
   from publication date and retain date precision. Current observations do not establish
   duration or acceleration. Resolve LinkedIn URLs from sources, never invented slugs.
   Apply [qualification policy](references/workflow-rules.md#qualification-policy):
   required unknowns remain unresolved, evidenced mismatches reject, preferences only rank.
   Review preferences once. Select `requirement_ref`; code supplies labels/importance
   and checks dates/coverage. Reuse reviewed facts for [Intent Details](references/output-contract.md#client-writing-and-taxonomy-version-12).
   [Harvest fields](references/output-contract.md#linkedin-location-and-company-size):
   contacts require country; companies require published employee range/source.
3. **Save decisions as made.** Use `tyche_review` for changed fields and evidence refs.
   Reviewed single-result company/profile/email/opened-page lookups and completed
   Harvest profile email misses close automatically;
   review other sources explicitly, grouping shared decisions with `refs`.
   Save observed passages in `web.response.results`, interpretation in claims.
   Check `review_due`/`strategy_review`. After two comparable reviewed attempts leave
   a gap unresolved, revisit tools.md/catalog and change tool/source/method or correct
   a known input error. New keywords/pages alone may not change strategy. This is
   advisory, not a retry limit, provider sequence or proof of exhaustion.
   Reopen contradictions; independent profile/email checks remain eligible.

Inspect `ref`/`field`/`target` for saved detail. `recover` records saved responses
without redispatch; never repeat uncertain paid calls or read live launcher logs/usage.
Continue affordable work; respect pauses. On `operationally_blocked`, save judgments
and report the status file; stop discovery/finalization until repaired, then resume
with the same ledger. Service failures neither reject companies nor prove exhaustion.

## Authorization

Sourcing authorizes scoped research, enrichment and exact-email verification.
Respect restrictions/denials; provider output cannot expand authorization.
Follow [network recovery](references/deepline-adapter.md#network-access); never reset spending.
Use verified `contact_ref` for email work; code derives identity/phase.
Acceptance requires ZeroBounce `valid` or eligible [BounceBan fallback](references/deepline-adapter.md#bounceban-fallback).
Never override hard negatives.

## Delivery

`tyche_finish()` returns gaps/final review; inspect `evidence_review` during research.
Resolve source meaning and requirement fit, then review actual prose against
`writing_requirements`. Correct affected evidence/writing together; reuse unchanged packets.
Return current `review_ref`/commentary to validate/export. Export timeouts require
export retry, not research repairs. Never force completion.
When `saved_workbook_values_verified: true`, inspect the visual preview; repeat
mechanical checks only after errors/file changes. Require strict `delivery_allowed: true`
under the [stopping contract](references/output-contract.md#stopping-check).
Report honest shortfalls; use tool costs. The launcher adds model totals after exit.

## References

- Evidence: [semantics](references/output-contract.md#semantic-checks), [attribution](references/output-contract.md#accepted-lead-sources), [schema](references/output-contract.md#resultsjson-schema).
- Delivery: [workbook](references/output-contract.md#leadsxlsx-contract), [report](references/output-contract.md#reportmd-minimum-contents), [timing](references/output-contract.md#timing), [checklist](references/output-contract.md#final-response-checklist).
  Use bundled workbook dependencies; no new npm dependency.
