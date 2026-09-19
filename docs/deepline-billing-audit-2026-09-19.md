# Deepline billing audit — September 19, 2026

TYCHE had several accounting defects, but some interruptions originate upstream.
This change separates a final price from ledger posting, reconciles individual
charges without splitting grouped totals, preserves decimal amounts, and detects
interrupted dispatches. It does not fabricate a charge to keep sourcing moving.

The audit and follow-up QA used 25 live executions across ten providers, with a single
500-credit authorization. Saved attributable evidence confirms **0.97 credits**;
three unresolved calls retain **30 credits of conservative audit holds**.
Those holds are not charges or proven provider maxima. No uncertain execution
was replayed, no historical ICP ledger was changed, and no account-balance delta
was attributed to this audit.

## Version and evidence

The initial probe set ran against main `9aa108867151a443dcdd16522e73a86962ee05c7`
with audit changes. Three subsequent calls exercised one shared version 2
ledger after rebasing onto `669f1bb9248c482db0a6a24c43a0d54f132198c0`.
The final implementation is based on main
`0aea1bb1ab71906cd05741a12231c02dceed2126` and retains its shared worker pool, admission checks, default budgets and billing-drain behavior.
It does not duplicate main's existing crash-released ledger locks or schema
redaction fixes.

Raw responses, catalog descriptions, exact billing snapshots and the shared
authorization ledger are saved locally under the audit checkout's
`reports/audit/`. `state.json` records each dispatch, and
`verified-probes.json` records normalization and attributable billing outcomes.
`verification-summary.log` is the human-readable counterpart. Raw account
records and credentials are not committed. Synthetic regression fixtures cover
the observed contracts; saved original responses were reinterpreted without
network execution or modification.

## Live results

Each row is one execution, including failed and free calls. A blank charge means
unknown, never zero. The status column uses the corrected parser on saved evidence.

| Probe | Operation | Result | Confirmed credits | Audit hold |
| --- | --- | --- | ---: | ---: |
| serper-miss | `serper_google_search` | No results; exact free usage record | 0 | 0 |
| serper-hit | `serper_google_search` | Two search results | 0.02 | 0 |
| firecrawl-page | `firecrawl_scrape` | One scraped page | 0.02 | 0 |
| harvest-github | `harvestapi_get_company` | Company returned | 0.03 | 0 |
| harvest-openai | `harvestapi_get_company` | Company returned | 0.03 | 0 |
| ark-hit | `ai_ark_company_search` | Company returned | 0.02 | 0 |
| context-free | `contextdev_get_web_scrape_markdown` | Page returned; exact free usage | 0 | 0 |
| ark-miss | `ai_ark_company_search` | No results; exact free usage | 0 | 0 |
| hunter-hit | `hunter_companies_find` | Company returned | 0.30 | 0 |
| hunter-miss | `hunter_companies_find` | HTTP 422; exact failed-zero usage | 0 | 0 |
| zerobounce-invalid | `zerobounce_validate` | Invalid-email verdict, billed | 0.28 | 0 |
| firecrawl-async | `firecrawl_batch_scrape` | Input rejected; exact failed-zero usage | 0 | 0 |
| firecrawl-async-valid | `firecrawl_batch_scrape` | Partial batch with two pages and final price | 0.03 | 0 |
| firecrawl-pending | `firecrawl_batch_scrape` | Async submission without a bill | — | 10 |
| firecrawl-status | `firecrawl_get_batch_scrape_status` | Completed page; free getter only | 0 | 0 |
| current-serper | `serper_google_search` | Two results; shared-ledger settlement | 0.02 | 0 |
| current-harvest | `harvestapi_get_company` | Transport failed without response or ID | — | 10 |
| current-ark | `ai_ark_company_search` | Transport failed without response or ID | — | 10 |
| qa-native-001 | `exa_search` | One result through native tools; exact posted charge | 0.10 | 0 |
| qa-native-002 | `ai_ark_company_search` | Company returned in concurrent native batch | 0.02 | 0 |
| qa-native-003 | `serper_google_search` | One result in concurrent native batch | 0.02 | 0 |
| qa-native-004 | `harvestapi_get_company` | Company returned in concurrent native batch | 0.03 | 0 |
| qa-native-005 | `contextdev_get_web_scrape_markdown` | Free-contract page; exact free usage record | 0 | 0 |
| qa3-native-001 | `parallel_search` | One result; exact posted charge | 0.02 | 0 |
| qa3-native-002 | `limadata_search_web` | Eight results after parser fix; exact flat call charge | 0.03 | 0 |
| **Total** | **25 executions / 10 providers** | **22 attributable / 3 unresolved** | **0.97** | **30** |

The published managed conversion is $0.10 per credit, so confirmed credits have
a nominal value of **$0.097**. This is separate from model usage, subscriptions,
provider-key billing and any contracted rates. Exact API USD, when supplied, is
retained separately and takes precedence over conversion. The public pricing
pages are context, not a replacement for per-request billing evidence:
[Deepline pricing](https://deepline.com/pricing) and
[per-task pricing](https://deepline.com/pricing/per-task).

## What changed

1. **Known price can settle before posting.** Positive live responses reported
   `pricing_status: final` with `settlement_status: queued`. These facts are now
   retained independently. Final prices also survive a partial result or an
   upstream timeout. A newly received amount without explicit finality remains
   pending; old saved receipts retain their original interpretation.
2. **Individual requests need individual bills.** Two Harvest calls appeared as
   one 0.06-credit usage aggregate but as two 0.03-credit ledger debits. Local
   reconciliation now reads the individual credit ledger before free/failed
   usage records, matching request ID, provider and operation. It rejects grouped
   totals, contradictory amounts, holds, refunds and duplicate final debits.
   Rounded display fields are never accounting inputs.
3. **Read-only recovery is bounded and resumable.** Both feeds use execution's
   credentials and host. Each read window shares a 30-second deadline, with at
   most four pages per feed and three persisted automatic attempts. A fresh head
   page checks delayed posting before the saved backlog. Production usage cursors
   repeated the same records with new opaque tokens; documented offsets advanced
   correctly. Offsets, repeated pages and cross-page overlap are now checked.
   A failed transport may use the other feed; malformed data or an organization
   change cannot silently fall back. Explicit billing-only resume adds bounded
   reads without spending, replaying research, or changing a limit.
4. **Cutoffs use exact decimals.** Wire amounts that cannot round-trip through a
   float are preserved as decimal strings. Enforcement and ledgers retain exact
   values; the existing numeric result schema is a reporting projection. Strict
   audit checks that projection against the canonical ledger and original proof.
   Historical USD-only overrun receipts can reconcile without invented credits.
5. **A crashed call cannot look live forever.** Each guarded dispatch owns an OS
   lease through settlement. Active parallel calls remain active; an orphaned
   `in_flight` entry becomes an unresolved liability for further admissions.
   No timeout expires or clears it. A stale snapshot whose call just settled is
   not misclassified as a crash.
6. **Valid provider output is not an input/billing error.** Observed Serper
   `organic` and AI Ark `content` lists now normalize correctly. Firecrawl's
   inner async state is separate from Deepline's outer completed/no-result label.
   A free status getter's bill never settles the original paid submission.
   Deepline's observed JSON-schema `type: any` shorthand is translated only at
   schema nodes; other constraints and literal data remain intact.
7. **Cost information is current and explicit.** Legacy native inspection reads
   the current ledger and model receipts instead of a cached report with obsolete
   model-cost fields. Reports distinguish unavailable, incomplete and complete
   model usage with `model_usage_status`; the numeric `estimated_llm_usd` remains
   the known subtotal for compatibility. Unavailable usage is not displayed as a
   zero-dollar estimate, and partial estimates are labeled incomplete.
8. **Pending billing is not an input-repair task.** Native progress retains the
   stop policy's reason. Finalization identifies pending provider billing or model
   usage and directs the researcher to save existing judgments and let the host
   reconcile. Admission rules, stop categories and the supervisor's billing drain
   are unchanged; no paid retry is added.
9. **Recovery has one owner per run.** Concurrent reconciliation previously made
   overlapping reads and could overwrite the saved retry history. A nonblocking
   OS guard now gives one caller ownership; peers return saved progress with an
   ephemeral `in_progress` flag. They do not reset retries or wait while holding
   the run's state lock. Host-side waiting polls local progress under its original
   deadline. Process exit releases ownership; no stale flag authorizes a retry.

Arena remains credential-free. Its broker uses the same exact billing parser on
the host-supplied response; it does not gain direct billing API access. Unknown
Arena charges still require authoritative evidence from its host.

## Remaining upstream limitations

- **Missing async bill:** the Firecrawl submission was saved at 08:31 UTC; a
  subsequent free getter showed its completed page. Production ledger and usage
  scans through that time still did not provide an attributable final charge.
  The 10-credit hold remains. Completing a job is not billing proof.
- **Lost correlation on transport failure:** two shared-ledger calls failed
  before any HTTP response or request ID was captured. They cannot be matched
  safely by tool name or timestamp. Their 20-credit hold remains. New receipts
  retain stage, exception type, elapsed time and errno without copying sensitive
  exception text. Those diagnostics cannot reconstruct the earlier failures.
- **Catalog drift:** Firecrawl's batch catalog omitted the `timeoutMs >= 5000`
  minimum enforced by its server. The rejected test was confirmed free. No
  speculative endpoint-wide price or schema exception was added.
- **Moving history:** offset pages can overlap while an account is busy. Exact
  overlaps are deduplicated, conflicting rows remain ambiguous, and automatic
  scans are deliberately bounded. A charge absent from the scanned window is
  still unknown. The code does not claim to audit every provider or an invoice.

A complete upstream fix needs an attributable final debit or explicit zero for
every accepted execution, including async completion and failed transport, plus
stable pagination and catalog constraints matching execution. Client-side code
can preserve uncertainty and avoid unnecessary model intervention; it cannot
safely manufacture that missing evidence.

## Verification

The regression suite exercises final versus estimated prices, partial/error
responses, exact zeros, grouped billing, missing IDs, paging failures, account
changes, legacy reconciliation, decimal boundaries, concurrent calls and process
crashes. The shared-ledger live probe passes strict accounting with its two
unresolved calls explicitly preserved. This is an accounting check, not a lead
delivery or official Arena qualification score.

Validation on the final main base, using Python 3.12:

- Sourcing suite: **1,009 passed**, with 10 workbook-runtime checks run separately.
  Two additional review regressions passed in the final **144-check** focused
  billing/HTTP/reconciliation/guard run.
- Workbook/finalization checks: **10 passed** (nine initial passes and one targeted
  rerun after correcting a stale sheet assertion). The same assertion failed on
  untouched main: it expected a Contacts sheet, while the documented exporter
  places every complete contact in Leads. No exporter behavior was changed.
- Arena, public-web and parallel-worker integration: **326 passed**, using the
  Leadpoet reference at `bde8a9182813296712220a267ada8539c6211246` and local fixture
  servers, without sourcing-provider calls.
- Launcher, shared-pool and run-cost checks: **74 passed**. These and the 326
  Arena checks were rerun together on final main: **400 passed**.

The 1,421 distinct checks cover accounting and the actual fixture delivery/export path.
They do not resolve the three missing live bills or certify an external invoice.
Detailed commands, selections and logs remain in the local audit directory.

## Follow-up user-journey QA

TYCHE's interface is the isolated Codex tool surface and its saved JSON, Markdown
and workbook outputs. This QA covers those interfaces; there is no separate web
application in this repository to certify.

- **Actual launcher and model:** `--check` and `--smoke` passed with local Codex
  `0.154.0`, Luna/high/Fast, six native tools and only the project-local sourcing
  skill. This disposable checkout initially lacked its pinned runtime; installing
  the documented local version resolved that setup check. No shared CLI was
  upgraded. The smoke made no provider calls; model usage is separate from the
  500-credit provider allowance.
- **Actual provider journey:** five native lookups on PR head `0868e0d` added
  Exa and exercised AI Ark, Serper and Harvest concurrently, then a catalog-free
  Context page. All five succeeded and matched attributable posted/free records:
  **0.17 credits / $0.017**, no new pending bills. Strict ledger audit returned no
  errors. An invalid Exa enum was rejected locally with allowed values and no
  ledger dispatch; the corrected request then succeeded. Native transcripts and
  receipts are retained in `reports/audit/qa-native-20260919b/`.
- **Delivery and visual output:** a persistent synthetic native journey accepted
  one fixture company with five contacts, passed strict delivery validation, and
  produced `leads.json`, `leads.xlsx`, `report.md` and a rendered workbook preview.
  The preview was inspected at original resolution: all five rows, repeated company
  fields, wrapped signals and intent details were readable. This is fixture QA,
  not five real prospects or an Arena score. Artifacts are retained in
  `reports/audit/qa-delivery-fixture-20260919/`.
- **Failure and recovery UX:** persistent native fixtures verify that a missing
  bill blocks new paid work and delivery, an exact later debit restores work,
  uncertain calls cannot replay, and the original clock and cap survive. A separate
  interrupted-model fixture names missing usage instead of asking for provider
  input changes. Saved transcripts are in `reports/audit/qa-failure-journeys-20260919/`.
  Report regressions cover absent, unpriced, partial, genuine zero and complete
  model usage, independently of pending provider billing.

No additional unresolved bill was introduced by this QA. The three earlier
upstream gaps and all historical ICP evidence remain unchanged.

First follow-up QA verification on main `0aea1bb` plus this PR's changes:

- **1,100 passed:** the full sourcing suite (including all workbook checks),
  native tools, launcher, shared pool and cost reports; no skips.
- **326 passed:** Arena, authoritative reference, public-web and parallel-worker
  integration. Three optional native wire cases were enabled separately.
- **3 passed:** actual pinned Codex against scripted local Responses/MCP servers,
  covering rejection by the older gateway contract, two large untruncated tool
  results, and the configured tool timeout. The wire fixture now uses the actual
  single-worker transport; its old synthetic run/session predated shared
  supervision. Shared supervision, quota and delivery checks remain in the Arena
  suite. No model inference or provider execution occurs in these three tests.

That is **1,429 passing checks** across the final QA selections, not the sum of
repeated runs. Independent review found no blockers in the accounting and UX
changes. Logs: `qa-full-suite.log`, `qa-arena-suite.log`, `qa-native-wire.log`.

## Concurrent recovery and HTTP fault QA

A second pass reproduced a concurrency defect on `1bacee3`: two simultaneous
recovery calls made **two billing reads but saved only one attempt**. With the
recovery guard, the same reproduction makes **one read, saves one attempt**, and
the peer returns `in_progress`. The guard is released if its owner dies. Windows
nonblocking contention is normalized to the shared busy exception; failed lock
acquisition no longer attempts to unlock a lock it never acquired.

Independent review also caught a deadline risk: recovery could wait for another
worker's state lock after acquiring its recovery guard. State updates now return
`in_progress` with `waiting_for: run_state` when busy. No state lock is held over
network I/O. If contention occurs after a bill arrives, the uncommitted charge and
page cursor remain pending for the next read; paid work is never replayed.
Settlement, derived route costs and page status are saved within the same state
section so another writer cannot leave the display behind the ledger. Independent
review verified both contention fixes.

Ten new regressions cover concurrent refresh/resume, state-lock ordering and
contention before and after a billing read, cross-process exclusion and owner
death, consistent settlement and display, preserved wait deadlines, failed lock
acquisition, Windows contention semantics, and two complete native HTTP journeys.
Those journeys use the actual adapter against localhost: a successful response
with delayed billing, a body lost after its correlation header arrived, a 503
billing-feed outage, and an explicitly free HTTP 422 rejection. Each saved charge
settles only from its matching bill, paid POSTs do not replay, and the original
budget and clock survive. Windows behavior is simulated; native Windows execution
was not available on this macOS host.

The original and corrected reproductions are retained in
`reports/audit/qa2-recovery-race-before.json` and
`qa2-recovery-race-after.json`. Native tool transcripts, raw synthetic HTTP
responses, ledgers and reports from the final code are retained in
`reports/audit/qa2-final-http-retained/`.
These fixtures make no live provider calls and are not real leads.

Fresh bounded reads of Deepline's ledger and usage feeds still confirm the same
**0.92 credits** across the earlier 23 executions; the **30-credit holds** remain.
This pass adds no provider execution or credit spend. The shared authorization
remains 500 credits, with model usage accounted for separately.

Final verification after these fixes: **1,439 passed**, comprising **1,110** sourcing,
workbook, native-tool and runtime checks plus **329** Arena/reference/parallel and
actual pinned-Codex wire checks. Nothing was skipped in these final selections.
Logs are `reports/audit/qa2-publish-full-suite.log` and `qa2-publish-arena-suite.log`;
fresh billing attribution is in `qa2-billing-verification.log`.


## Slow-response and additional-provider QA

A third pass reproduced a response-timeout defect on `ba4e708`: a server sending
bytes every 50 ms kept both execution and billing reads alive for about 1.1 seconds
with a 0.2-second timeout. The HTTP response reader now shares an absolute deadline
across header, body and chunk parsing. The same reproduction stops at about 0.203
seconds. It preserves available request IDs, keeps interrupted charges pending,
and never retries a paid POST. Standard urllib TLS verification, HTTP error bodies,
content-length checks and proxy configuration are preserved.

Six new regressions cover slow bodies, slow headers, slow chunk metadata, complete
and truncated responses, TLS defaults, and pending accounting after a timeout.
A separate real HTTPS fixture passed with certificate and hostname verification
on. This bounds response reads; DNS, connection establishment, request upload and
proxy CONNECT retain the existing transport behavior. It is not a claim of a hard
whole-request deadline across every network phase. Evidence: `qa3-slow-http-before.json`,
`qa3-slow-http-after.json`, `qa3-https.json` and `qa3-deadline-regressions.log`.

Two new native executions exercised Parallel and Limadata. Both returned final
prices that matched individual posted debits: **0.02 + 0.03 = 0.05 credits**, with
no new pending bills. The local batch guard first rejected discovery calls in an
array before dispatch; the harness continued them as individual lookups with the
same saved clock and ledger. No uncertain request was replayed.

Limadata returned eight valid organic search rows, which the previous parser
reported as `schema_error` while correctly retaining the flat call charge. A narrow
adapter for that observed envelope now preserves the rows and their source links.
Three regressions cover the flat price, empty results and malformed data. Invalid
or blank source URLs/titles remain schema errors while retaining valid final bills. Re-reading
the immutable saved responses through native tools exposes eight Limadata rows and
one Parallel row, with **$0.005** and zero pending calls; strict accounting passes.
That offline fixture performs no live executions, and its mirrored cost must not
be added to audit spending. Original response receipts remain unchanged.

Live receipts and transcripts are in `reports/audit/qa3-native-20260919/`.
The final offline native transcript, cost report and proof are in
`reports/audit/qa3-saved-native-verified/`. Fresh billing attribution is saved in
`qa3-billing-verification.log`. The audit now has 25 executions across ten providers,
**0.97 credits confirmed**, and the same **30-credit holds** for three older gaps.
The shared authorization remains 500 credits; model usage remains separate.

Final selections after both fixes: **1,448 passed** — **1,119** sourcing, workbook,
native-tool and runtime checks plus **329** Arena/reference/parallel and real
pinned-Codex wire checks. No tests were skipped in these selections. Logs:
`qa3-publish-full-suite.log` and `qa3-publish-arena-suite.log`. Both fixes passed independent review; all prior billing guards remain active.
