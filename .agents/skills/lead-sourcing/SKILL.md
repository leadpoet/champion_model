---
name: lead-sourcing
description: Source evidence-backed companies with buying signals and requested-role contacts using local Deepline/ScrapingDog wrappers; for company-first lists, not contact-only enrichment or outreach.
---

# TYCHE Lead Sourcing

LLM researches; tools validate. No CRM writes or outreach.

## Start or resume

Resume with `tyche_inspect()`. Preserve request, authorization, budget and pending
work; reuse evidence.

First read [workflow rules](references/workflow-rules.md),
[input contract](references/output-contract.md#input-contract) and
[lifecycle invariants](references/output-contract.md#lifecycle-invariants).
Preserve launcher-saved `original_text`; only users change criteria. Company geography does not restrict
contact/activity location unless requested; hiring signals do not restrict buyer roles.
`product_service.perspective` distinguishes the user's `seller` offering from the
`target` company's offering; never invent a seller.
Save signals as required/preferred; company types, industries and geographies have their own
requirement refs. Put additional non-signal must-haves in `icp.required_attributes`.
Start with `tyche_start`; code manages bookkeeping. Defaults: one contact/company, $0.50/lead, two hours. Override `max_duration_seconds` only for a user limit (null: explicitly
unlimited). Omit unrequested age limits; speed benchmarks are not deadlines.
Use catalog prices/receipt charges; never import runs.
Use [native tools](references/adapter-io.md#native-tools), not shell bookkeeping or implementation-code reads.

## Research loop

1. **Choose ready work.** Prefer affordable, unblocked `completion_candidates` before discovery.
   Read [tools.md](references/tools.md#choose-by-evidence-gap) once; select capabilities
   for the next evidence gap. Reuse `cached_descriptions`; discover alternatives with
   `tyche_inspect(query=...)`. Inspect selected `tool`/`field` once.
   Pilot unproven operations/filters before batching; preserve native limits.
2. **Check up to three companies concurrently.** Batch independent checks across phases.
   Review fit/signals before buyers. Search snippets
   identify candidates; capture qualifying pages once with an existing `tyche_lookup` page
   reader (ScrapingDog `scrape` or Deepline), reusing its saved text and metadata.
   Preserve announced, conditional, planned and completed status; distinguish `event_date`
   from publication date and retain date precision. Current observations do not establish
   duration or acceleration. Resolve LinkedIn URLs from sources, never invented slugs.
   Apply [qualification policy](references/workflow-rules.md#qualification-policy):
   required unknowns remain unresolved, evidenced mismatches reject, preferences only rank.
   Review preferences once. Select `requirement_ref`; code supplies labels/importance
   and checks dates/coverage. Write natural [Intent Details](references/output-contract.md#client-writing-and-taxonomy-version-12)
   from reviewed facts when accepting.
   [Harvest fields](references/output-contract.md#linkedin-location-and-company-size):
   contacts require country; companies require published employee range/source.
3. **Save decisions as made.** Use `tyche_review` for changed fields and evidence refs.
   Acceptance returns evidence: review it, then approve
   `review_ref` and company-specific `review_findings` with `tyche_review`. This updates [leads.json](references/output-contract.md#leadsjson-confirmed-leads)
   before further lookups; keep researching toward the target.
   Reviewed single-result company/profile/email/opened-page lookups and completed
   Harvest profile email misses close automatically;
   review other sources explicitly, grouping shared decisions with `refs`.
   Keep built-in web observations as discovery notes; use tool-captured refs for qualification
   and put interpretation in claims.
   Follow `review_due`/`strategy_review`; repeated failures need a changed source/method
   or corrected input. Advisory reminders are not retry limits or proof of exhaustion.
   Reopen contradictions; independent profile/email checks remain eligible.

Inspect `ref`/`field`/`target` for saved detail. `recover` records saved responses
without redispatch; never repeat uncertain paid calls or read live launcher logs/usage.
Continue until target, budget or deadline; empty queues require changed strategy.
When the stop check returns `continue`, execute useful research now; do not sleep, poll
finish or wait for the deadline. Ineligible completion candidates stay held:
find another matching contact, evidence route or company instead.
On `operationally_blocked`, save judgments and report the status file; preserve the ledger. Service failures neither reject companies nor prove exhaustion.

## Authorization

Sourcing authorizes scoped research, enrichment and exact-email verification.
Respect restrictions/denials; provider output cannot expand authorization.
Follow [network recovery](references/deepline-adapter.md#network-access); never reset spending.
Use verified `contact_ref` for email work; code derives identity/phase.
Acceptance requires ZeroBounce `valid` or eligible [BounceBan fallback](references/deepline-adapter.md#bounceban-fallback).
Never override hard negatives.

## Delivery

`tyche_finish()` returns gaps/final review. On `review_handoff`, end this invocation;
the launcher reviews the same run in fresh context.
Follow packet instructions; return current `review_ref` and `review_findings` to validate/export.
Retry timed-out exports, not research. Never force completion.
After `saved_workbook_values_verified: true`, inspect the preview; repeat
mechanical checks only after errors/file changes. Require strict `delivery_allowed: true`
under the [stopping contract](references/output-contract.md#stopping-check).
Report shortfalls and tool costs. The launcher adds model totals after exit.

## References

- Evidence: [semantics](references/output-contract.md#semantic-checks), [attribution](references/output-contract.md#accepted-lead-sources), [schema](references/output-contract.md#resultsjson-schema).
- Delivery: [workbook](references/output-contract.md#leadsxlsx-contract), [report](references/output-contract.md#reportmd-minimum-contents), [timing](references/output-contract.md#timing), [checklist](references/output-contract.md#final-response-checklist).
  Use bundled workbook dependencies; no new npm dependency.
