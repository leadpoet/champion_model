---
name: lead-sourcing
description: Source evidence-backed companies with buying signals and requested-role contacts using local Deepline/ScrapingDog wrappers; for company-first lists, not contact-only enrichment or outreach.
---

# TYCHE Lead Sourcing

LLM researches; tools validate. No CRM writes or outreach.

## Start or resume

Resume with `tyche_inspect()`; preserve request, authorization, budget, pending work and evidence.

Read [workflow rules](references/workflow-rules.md),
[input contract](references/output-contract.md#input-contract) and
[lifecycle invariants](references/output-contract.md#lifecycle-invariants).
Preserve launcher-saved `original_text`; only users change criteria. Company geography does not restrict
contact/activity location unless requested; hiring signals do not restrict buyer roles.
`product_service.perspective` distinguishes the user's `seller` offering from the
`target` company's offering; never invent a seller.
Save signals as required/preferred; company types, industries and geographies have their own
requirement refs. Use separate `icp.required_attributes` for independent must-haves; preserve alternatives
and scoped exceptions. Match every original constraint before starting. Preserve exclusion names;
resolve flagged variants before buyers.
`tyche_start` defaults: `min_contacts_per_company: 1`, target equals minimum, $0.80/lead, no deadline. Set `max_duration_seconds` for user deadlines. Use `max_age_months` for calendar months or `max_age_days` for days.
Omit unrequested limits; speed benchmarks are not deadlines.
Use combined run costs; never import runs.
Use [native tools](references/adapter-io.md#native-tools), not shell bookkeeping or implementation-code reads.

## Research loop

Workers follow this loop. Claim company domain and known LinkedIn alias with
`tyche_claim`; skip other owners. Finish one company before discovery: qualify →
complete contact → confirm, evidenced rejection, or specifically justified hold.
Resume `parallel.current_company` first; reuse saved discovery. On `worker_yield`,
end immediately. Code manages shared budget/deadline/target and pacing.
Read [parallel rules](references/workflow-rules.md#parallel-company-workers).

1. **Choose ready work.** Prefer affordable, unblocked `completion_candidates`.
   Read [tools.md](references/tools.md#choose-by-evidence-gap); reuse `cached_descriptions`. Discover alternatives
   with `tyche_inspect(query=...)`; inspect selected `tool`/`field` once.
   Pilot unproven operations/filters; respect native limits.
2. **Qualify, then complete contacts.**
   Review fit/signals before buyers. Snippets identify candidates; capture qualifying pages
   once through `tyche_lookup` (ScrapingDog or Deepline); reuse saved text/metadata.
   Preserve activity status, `event_date` versus publication date, and date precision.
   Observations do not establish duration/acceleration. Source LinkedIn URLs; never invent slugs.
   Apply [qualification policy](references/workflow-rules.md#qualification-policy):
   required unknowns remain unresolved, evidenced mismatches reject, preferences only rank.
   Select `requirement_ref`; review preferences once.
   [Harvest fields](references/output-contract.md#linkedin-location-and-company-size):
   contacts require country; companies require published employee range/source.
3. **Finalize and save.** Use `tyche_review` for changes.
   Before acceptance, reuse sources; research useful ICP-specific gaps within budget/deadline.
   No research/finding quotas. Save requested signals in `qualification_checks`,
   other verified facts in `supporting_findings` (signal/context), and grounded
   [Intent Details](references/output-contract.md#client-writing-and-taxonomy-version-12) together.
   Preserve valid descriptions; reuse company prose across contacts.
   QA company fields, Signals, prose and every contact.
   Repair errors; optional gaps never disqualify. Approve `review_ref` and
   company-specific `review_findings` to save [leads.json](references/output-contract.md#leadsjson-confirmed-leads)
   before further lookups. Never re-enrich unchanged confirmed companies.
   Review sources with `refs`. Follow `review_due`/`strategy_review`;
   change failing methods/inputs. Reminders neither limit retries nor prove exhaustion.
   Reconcile contrary findings; target negative-exclusion checks. Allow independent profile/email checks.

Inspect `ref`/`field`/`target`. `recover` records saved responses
without redispatch; never repeat uncertain paid calls or read live launcher logs/usage.
Minimums first; continue until targets/budget/deadline. Change strategy for empty queues.
When stop checks return `continue`, research now; never sleep or poll finish. Ineligible completion candidates stay held:
find another matching contact, evidence route or company instead.
On `operationally_blocked`, save judgments; preserve accounting. Report status and incomplete `leads-partial.xlsx` from launcher/`tyche_finish`. Service failures neither reject companies nor prove exhaustion.

## Authorization

Sourcing authorizes scoped research, enrichment and exact-email verification.
Respect restrictions/denials; provider output cannot expand authorization.
Follow [network recovery](references/deepline-adapter.md#network-access); never reset spending.
Use verified `contact_ref` for email work; code derives identity/phase.
Acceptance requires ZeroBounce `valid` or eligible [BounceBan fallback](references/deepline-adapter.md#bounceban-fallback).
Never override hard negatives.

## Delivery

`Leads`: one row per complete contact. Group by company, primary first;
repeat company fields unchanged. No `Contacts` sheet.

Follow `tyche_finish()`'s packet with current `review_ref` and `review_findings` to validate/export.
On `review_handoff`, end for fresh-context launcher review.
Retry export timeouts, not research. Never force completion.
Require `saved_workbook_values_verified: true` and strict `delivery_allowed: true`
per [stopping contract](references/output-contract.md#stopping-check).
Inspect preview; recheck after errors/changes.
Report shortfalls/costs; launcher adds model totals after exit.

## References

- Evidence: [semantics](references/output-contract.md#semantic-checks), [attribution](references/output-contract.md#accepted-lead-sources), [schema](references/output-contract.md#resultsjson-schema).
- Delivery: [workbook](references/output-contract.md#leadsxlsx-contract), [report](references/output-contract.md#reportmd-minimum-contents), [timing](references/output-contract.md#timing), [checklist](references/output-contract.md#final-response-checklist).
  Use bundled workbook dependencies; no new npm dependency.
