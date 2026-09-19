# TYCHE in the Leadpoet lab

Local tests and Arena use **one runner**, `scripts/codex_tyche.py`. Both use
Codex 0.154.0, Luna, high reasoning, the project sourcing skill, native research
tools, saved-response recovery, continuation prompts and finalization decisions.
`tyche_arena/runtime.py` and its separate research loop/strategy have been removed.

`harness.run_icp` only initializes Arena's authoritative request and invokes that
runner through `tyche_arena/host.py`. The host adapter starts Codex through the
existing `lab_arena_codex.session`, supplies brokered provider transport, preserves
reviewed checkpoints and validates Arena JSON. Local execution keeps its personal
Codex authentication, usage receipts, workbook and preview. Arena keeps its
OpenRouter route, isolated credentials, sandbox, accounting, quotas and scoring.
Local Fast service tier is a personal-account setting; it is not sent to Arena.

```text
Local CLI ───────┐
                ├─ main TYCHE supervisor → shared skill and research tools
Arena run_icp ──┘                          → environment-specific transport/output
```

Arena's session opts into hosted web search. This requires the accompanying
Leadpoet host change: a bounded Responses search tool, citation/history validation
and search-cost admission. Other Arena session callers keep search disabled.
OpenRouter's native preference can fall back to Exa according to provider support;
identical search results or identical provider execution are not guaranteed.
Search citations remain discovery evidence; native receipt and qualification
checks still decide whether a source supports a lead.

The Arena bundle preserves the published adapter fixes from sales-agent lab
`5e6d881882a2dae2be3ca783060f33e0380dd0de`, while using the current main research
implementation. Source publishing does not deploy the host or promote a baseline.
A live deployed journey remains a separate release check.

Arena allows 2,070 seconds of research and 600 seconds for finalization, within
its existing 2,670-second worker bound and 2,700-second outer limit. The shared
runner preserves the original clock and receipts across invocations. Quota
exhaustion stops with an explicit host limit and retains reviewed output; it does
not wait until the deadline to manufacture an ordinary research stop. Repeated
clean exits without saved progress also stop after five invocations in both modes.

## Growing JSON and partial completion at cost/time limits

After accepting each company, `tyche_review` returns its source packet. Codex
reviews it and approves the current `review_ref` through `tyche_review`. That same
operation saves native `leads.json`, maps only confirmed rows to Arena's schema,
and publishes `/output/companies.json` through the host's atomic checkpoint writer.
No separate `tyche_checkpoint` call is required; that tool remains compatible for
existing callers. Qualification, original sources, contacts, email provenance and
accounting checks remain enforced. The original target and budget stay unchanged.

The compatibility checkpoint tool reviews its partial snapshot in the current
session, including when research would hand final review to a fresh context.
It restores that phase afterward; ordinary finish keeps the native handoff.

The file therefore grows from one confirmed company to two and onward while
research continues. At a provider or model cost cutoff, ICP deadline or worker
failure, the last successfully published list is already available. Do not wait
until shutdown to export it: the host can stop the process immediately. A failed
publication blocks the next lookup until publication succeeds; retry or MCP
restart reuses the approved JSON without repeating paid calls. Changed or withdrawn
leads are removed until reviewed again. Unfinished companies stay in research state.

The host JSON is the publication commit. On process exit or a local timeout,
TYCHE validates its exact contents against the saved approvals and original
receipts. Native `leads.json` is saved before publication, so even the first host
save can be recovered if a later local write fails. `checkpoint-results.json`
also preserves the preceding publication. Local `companies.json` and
`validation.json` are diagnostic copies; an interrupted write to either cannot
invalidate valid host output. Newer approvals that never reached the host are
not silently included in its recovered list.

Recovery checks those published leads against both the current research state
and saved confirmations. Changed or withdrawn leads are removed, and the reduced
list must reach the host before TYCHE returns it. If that write fails, TYCHE raises
an error instead of returning the stale list. Recovery makes no model or provider
calls. An unrelated unfinished candidate or later uncertain billing still does
not invalidate unchanged confirmed leads.

For rounds using `atomic_checkpoint_45m_v1`, Arena's runtime keeps the last valid
checkpoint it completely read **before** the signed deadline, including when it
kills the sandbox. It does not recover unsaved drafts or output written after the
deadline. Two completed, reviewed companies out of a target of five can therefore
enter normal scoring; the remaining three are unfulfilled. Factual qualification,
contact provenance, duplicates and the round's scoring policy still determine
credit. TYCHE's ordinary finish path is still available to close a completed run.
TYCHE cannot retract a checkpoint already retained by the external Arena after a
hard kill or an unavailable output mount. Revocations must reach the host before
its signed deadline; the recovery behavior above applies while TYCHE can run.

## Input and output

- Supports `intent_details_v1` and `contacts_v1`, with the lab's v5 company
  output and `LAB_ARENA_COMPANY_LIMIT` of 1–5.
- Keeps the original ICP, roles, exclusions, company criteria, required
  attribute, contact geography and seniority. Primary signals and required
  attributes must be text. The primary signal at index 0 is mandatory.
  Generated `bonus_intents` remain optional, preserve scoring order, and use
  their individual age limits.
- Translates structured company criteria into the current native request fields.
  The complete ICP, including its prompt, stays in `original_text`; new runs do
  not populate the retired `icp.custom_criteria` field.
- Signal dates use the reviewed activity's `event_date`. A current observation
  may use its observation date. Publication dates never replace activity dates,
  and partial or unknown dates emit `null` instead of an invented day.
- Contact email must appear in the selected HarvestAPI `get_profile` receipt
  requested with `findEmail: "true"`. Its actual provider record ID is emitted
  as `contact.email_source.record_id`. Local route IDs and Deepline request IDs
  are not substituted for lab broker call IDs. The lab's verifier remains the
  authority on factual fit and provenance.
- Incremental confirmation, finish and checkpoint check the Arena output mapping before requesting
  evidence approval. Missing provider provenance or an overlong intent paragraph
  returns an actionable repair result before any approval or publication.
- `tyche_finish` produces the existing evidence packet. Oversized company review
  packets are available through `tyche_inspect(target=..., field="evidence_review",
  offset=...)` in complete JSON pages. Check the content hash and total length
  across pages, read them all, and then explicitly approve the current
  `review_ref`. Paging itself never approves evidence.
- Approval runs strict validation, maps only accepted records, calls
  `lab_arena_checkpoint.write`, and saves `companies.json`, `validation.json`
  and the checkpoint snapshot after the host write succeeds.
  Delivered state is closed to further research changes.
- After Codex exits or its local timeout fires, the harness validates host JSON
  against saved approvals, the original ICP and current lead state. It returns
the company list for the lab's normal entrypoint. Final text alone is never
  delivery. Checkpoints contain only reviewed output; the adapter does not
  periodically publish unreviewed drafts.

## Provider boundary

Arena routes Deepline and supported ScrapingDog requests through the existing
worker broker. Catalog reads are local. `tyche_open` captures exact public pages
through the host proxy and retains run-bound receipts; authored notes and
finalization rereads cannot become qualifying research evidence.

Both paid providers share the USD 0.80-per-requested-company allowance, subordinate
to host accounting. Unknown billing pauses further paid research. Arena owns
model charges and the combined cutoff; no personal-account model receipts are
created. Adapter call counts are telemetry, never a second quota. Socket waits
are bounded and interrupted paid requests are never replayed.

## Package and enable

```sh
python3 scripts/build_arena_bundle.py /tmp/tyche-codex-bundle
```

The builder stages an allowlist of source files, shared instructions/references,
taxonomy assets, the public catalog, `requirements.txt` and the license. It
excludes reports, local settings, credentials, tests and Git history. Install
`requirements.txt`: `geonamescache` validates country/region names and `jsonschema`
checks provider inputs against their saved live schemas before paid dispatch.
Submit the staged directory through the existing lab source-bundle
and baseline promotion process; do not install the desktop launcher in the lab.

Before enabling a round, Leadpoet PR #198 needs passing required checks, then
merge and deployment of its Codex-equipped image and cost-reconciliation
migration `263-lab-arena-codex-cost-reconciliation.sql`. The migration-number
collision is resolved in PR #198; its SQL is unchanged.
The selected round must admit the model and install this source bundle's
dependencies. Existing rounds retain their frozen baseline. This TYCHE PR does
not deploy, promote a baseline, change subnet infrastructure, or modify PR #198.

To refresh free public tool metadata after the lab allowlist changes:

```sh
python3 scripts/refresh_arena_catalog.py /path/to/leadpoet
```

## Verification and limits

The initial protocol audit used PR #198 commit
`2558d4bc418046ac9146c7992032405034150601`; checkpoint cutoff and scoring behavior
were also read at `8f12c82ed47dd7553ea986133fdfee84158675ad`. The CI repair diff
through `2db39588bdf6f6cad8d9ccbfef2026eeeef6a546` preserves that runtime contract.
Session signatures, mounted paths,
Responses allowlist/limits, provider frames, checkpoint writer and receiver
input/output contracts were read as source, without importing or executing Leadpoet.

```sh
python -m pytest tests/test_arena_codex.py -q
python -m unittest discover -s .agents/skills/lead-sourcing/tests -p 'test_*.py'
# Optional: the exact 0.154.0 binary with its sibling codex-code-mode-host.
TYCHE_TEST_CODEX_BINARY=/path/to/codex python -m pytest tests/test_codex_wire.py -q -rx
```

TYCHE-only offline fixtures cover the trigger through reviewed saved output,
MCP configuration/schema bounds, primary/bonus semantics, stale evidence,
wrong email provenance, false text completion, changed checkpoints, quotas,
uncertain billing, checkpoint failure and detached MCP cleanup. Adapter tests
also cover one completed company out of five surviving timeout/error with an
unfinished candidate and subsequent billing uncertainty; current review approval;
replacement checkpoints; and blocked partial delivery with invalid evidence or contacts.
Compatibility cases cover current request fields, event versus publication dates,
repair before approval, research-phase checkpoints, complete large review pages,
and raw API receipts through the same normalizer used locally and during recovery.
These tests use fixture processes, checkpoints and provider replies. The optional native
tests run the actual Codex CLI and code-mode companion with scripted loopback
responses: two MCP calls, continuation and forced context compaction. A separate
case records the original PR #198 contract rejection as an expected failure.
That historical fixture does not validate the updated upstream protocol.

Actual Codex-to-MCP execution inside the deployed lab image, model availability,
live provider behavior and sourcing quality remain unverified. A lab smoke run
is required after the protocol fixes before promotion; unit checks cannot prove
that deployed journey. It was not run as part of this code-only integration.

## Shared-runner boundary

Research policy belongs in the shared skill, native research tools or main
supervisor. Authentication, external transport and output projection belong in
the environment adapter. Do not add another Arena continuation loop or strategy
prompt. Host limits and output eligibility remain authoritative.
