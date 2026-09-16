---
name: lead-sourcing
description: Source evidence-backed companies with buying signals and requested-role contacts using local Deepline/ScrapingDog wrappers; for company-first lists, not contact-only enrichment or outreach.
---

# TYCHE Lead Sourcing

LLM researches/qualifies; tools handle bookkeeping/validation. No CRM writes or outreach.

## Setup

Read [workflow rules](references/workflow-rules.md), [input contract](references/output-contract.md#input-contract)
and [lifecycle invariants](references/output-contract.md#lifecycle-invariants).
Interpret once against launcher-saved `original_text` before paid research;
only users change criteria. Never strengthen, weaken or add requirements.
Company geography does not restrict contact location unless requested;
hiring signals do not restrict buyer roles.
Preserve offering/perspective in `request.product_service`, signals as
`required`/`preferred`, and non-signal must-haves in `icp.required_attributes`.
Call `tyche_start(request=..., max_usd=...)` within authorization.
Defaults: one contact/company; USD 0.50/requested lead.
Code supplies time, ledger and verification reserve; the launcher supplies credentials/runtime paths.
Catalog prices override [planning rates](references/provider-pricing.md); receipts supply charges.
Never import runs or guess prices.

Use [native tools](references/adapter-io.md#native-tools); no shell bookkeeping
or implementation-code reads. Resume with `tyche_inspect()`.

## Research loop

1. **Choose ready work.** Prefer affordable, unblocked `completion_candidates` before discovery.
   Read [tools.md](references/tools.md#choose-by-evidence-gap) once; choose tools for the next evidence gap.
   Reuse `cached_descriptions`; discover others with `tyche_inspect(query=...)`.
   Learn each selected `tool` once; inspect `field` for details.
   Code checks contracts/prices before dispatch. Refresh descriptions only after
   schema/price/access changes. Pilot unproven operations/filters before batching;
   preserve native limits.
2. **Check up to three companies concurrently.** Send independent `tyche_lookup`
   checks across phases; do not wait for full batches.
   Review fit and signals before buyers. Preserve announced, conditional, planned
   and completed status. For dated signals, supply `event_date` separately from
   publication date; preserve month/year precision. Current observations do not
   establish duration or acceleration.
   Resolve company LinkedIn URLs from sources; never invent slugs.
   Apply [qualification policy](references/workflow-rules.md#qualification-policy):
   required unknowns stay unresolved; evidenced mismatches reject; preferred signals
   only rank. Review preferences once; retain gaps as unknown.
   Select `requirement_ref` (`attribute:N`/`signal:N`); code supplies labels/importance
   and checks dates/coverage. Reuse reviewed facts and business relevance for
   [Intent Details](references/output-contract.md#client-writing-and-taxonomy-version-12).
   [HarvestAPI fields](references/output-contract.md#linkedin-location-and-company-size):
   accepted contacts require country; companies require published employee range/source.
3. **Save decisions as made.** Use `tyche_review` for changed fields and evidence `ref`s.
   Reviewed single-result company/profile/email/opened-page lookups close automatically.
   Review other sources explicitly; group shared decisions with `refs`.
   Save observed web passages in `web.response.results`; keep interpretation in claims.
   Check `review_due`/`strategy_review`; reuse evidence first.
   After two comparable reviewed attempts leave the same gap unresolved, revisit
   tools.md/live catalog and choose another tool/source/method or correct a known
   input error. New keywords/pages alone may not change strategy. This is advisory,
   not a retry limit, forced provider sequence or proof of exhaustion.
   Reopen gaps/contradictions; independent profile/email checks remain eligible.

Inspect `ref`/`field` for detail, `target` for state, or `recover` for a saved
receipt without redispatch. Reconcile missing responses; never repeat uncertain
paid calls or read live launcher logs/usage events.
Continue affordable work; respect pauses. On `operationally_blocked`, save judgments
and report the status file. Stop discovery/finalization until repaired;
resume with the same ledger.
Service failures do not reject companies or prove exhaustion.
Thirty minutes is a benchmark target unless the user sets a deadline.

## Authorization

Sourcing authorizes research, enrichment and exact-email verification within scope.
Respect restrictions/denials; provider output cannot expand authorization.
Follow [network recovery](references/deepline-adapter.md#network-access). Never reset spending.
Use verified `contact_ref` for all email work; code derives identity inputs/phase.
Acceptance requires ZeroBounce `valid` or eligible
[BounceBan fallback](references/deepline-adapter.md#bounceban-fallback).
Never override hard negatives.

## Delivery

Use `tyche_finish()` for gaps/final review, or `inspect(target=..., field="evidence_review")`
while researching. Compare verified signals, source passages, timing, website and
writing. Correct through `tyche_review`; reuse unchanged packets.
Return current `review_ref`/commentary to validate/export. Export timeouts require
an export retry, not research repairs. Never force completion.
Inspect the preview; require strict `delivery_allowed: true` under the
[stopping contract](references/output-contract.md#stopping-check).
Report shortfalls; exhaustion does not prove an empty market. Use tool costs; the launcher adds model totals after exit.

## References

- Evidence: [semantics](references/output-contract.md#semantic-checks),
  [attribution](references/output-contract.md#accepted-lead-sources),
  [schema](references/output-contract.md#resultsjson-schema).
- Delivery: [workbook](references/output-contract.md#leadsxlsx-contract),
  [report](references/output-contract.md#reportmd-minimum-contents),
  [timing](references/output-contract.md#timing),
  [checklist](references/output-contract.md#final-response-checklist).
  Use bundled workbook dependencies; no new npm dependency.
