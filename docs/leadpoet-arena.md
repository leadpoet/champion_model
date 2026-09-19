# TYCHE in the Leadpoet lab

**Promotion remains unverified:** PR #198 at `2db3958` passes its native protocol
checks, full test suite and gateway/Arena image builds in
[upstream CI](https://github.com/leadpoet/leadpoet/actions/runs/35040315045).
The deployed lab journey has not been exercised. See
[the compatibility audit](leadpoet-codex-audit.md) for the historical findings
and upstream acceptance checks. Offline delivery tests do not establish
compatibility with a deployed broker or guarantee sourcing quality.

This bundle implements `harness.run_icp(icp) -> list[dict]` for the Codex lab
runtime in [Leadpoet PR #198](https://github.com/leadpoet/leadpoet/pull/198).
Codex drives TYCHE's existing research tools and source-review workflow. The lab
provides the executable, isolation, model transport, credentials and budgets.
There is no additional model SDK, agent framework or running service to deploy.
The local TYCHE launcher and its workbook delivery remain unchanged.

## Execution

```text
Lab calls harness.run_icp(icp)
  → create isolated request, ledger and receipts under /tmp
  → lab_arena_codex.session → /usr/local/bin/codex exec
  → native TYCHE MCP tools → lab worker → Deepline
  → review each completed company → validate → atomic JSON checkpoint
  → continue research → final delivery or deadline
  → revalidate the last published snapshot → return companies to the lab
```

The adapter calls PR #198's `session(model=..., reasoning_effort=...)`, adds
the TYCHE MCP configuration to that session's isolated `CODEX_HOME`, and runs
one Codex process while the session remains open. PR #198 owns the Responses
bridge and sends `openrouter.responses` through the lab worker. TYCHE never
implements or replaces that model transport.

TYCHE's research tools and a lab-only `tyche_checkpoint` tool use its shared MCP
transport. The host initializes the request, so `tyche_start` is unavailable
to the model. The lab already isolates the process in gVisor; the adapter does
not invoke the desktop launcher's nested sandbox relay. Shared qualification,
email, accounting, stopping and final evidence-review gates still apply.
The lab delivers reviewed JSON and can save completed companies before the
whole run is ready to stop. The desktop finish path still requires its strict
whole-run preflight and workbook delivery.

The default is `openai/gpt-5.6-luna` with `xhigh` reasoning, matching the local
launcher's model family and effort. The round must include that model in its
price table and support it through OpenRouter Responses. OpenRouter's public
catalog lists this exact model and `xhigh`; its native Responses behavior and
round admission have not been verified with a paid call. The local launcher's Fast setting is omitted:
PR #198's closed request schema does not accept `service_tier`. There is no
automatic fallback to another model or personal Codex login.
Native Codex model metadata and code-mode behavior are retained. Explicit
`agents.enabled=false` and `features.multi_agent_v2=false` keep this a single
research worker; `features.multi_agent=false` alone does not override Luna's
model metadata. Image generation is disabled for this text-only workflow.
The adapter leaves context compaction and tool-output limits to the host session
and native model defaults; it does not impose a separate 16K context threshold.

The bundle refuses execution outside `/agent/source` or without the lab's
two socket mounts, host-mounted runtime helpers, executable and output path.
It is for new parallel-execution lab rounds, not historical or local runs.
The research deadline is 2,250 seconds; the Codex process is bounded at 2,670
seconds, reserving seven minutes for final review inside the lab's 2,700-second
window. The outer signed deadline and quotas always remain authoritative.
Timeout/error paths close the
session, kill the process group and save bounded diagnostics. The MCP process
also watches its Codex parent because Codex gives MCP a separate process group.
They never relaunch a potentially billed call or silently deliver unfinished records.

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

Provider research uses only the lab's `deepline.execute` operation. The
bundled public catalog covers the 21 approved tools at the inspected PR #198
revision. Metadata reads are local; no Deepline CLI installation is needed
inside the lab. ScrapingDog and manually injected web observations are not
exposed by this adapter. There is no direct-provider fallback.

CLI responses, Arena responses and saved-response recovery all use the shared
Deepline normalizer. It recognizes the observed completed-job Exa answer and
empty HarvestAPI company envelopes. Exa citation passages remain separate from
the generated answer; a title-only citation does not acquire a fabricated body.
Unknown shapes retain the existing parser behavior. Raw receipts and billing
are unchanged, and normalization never repeats the provider call.

Raw provider receipts, billing and request identities are retained.
New runs use observed provider charges without monetary reservations. Unknown
billing pauses additional paid research. Arena owns model usage and the combined
cutoff; this adapter does not fabricate a local model charge.
The local provider allowance is USD 0.50 per requested company; model costs
are separate and enforced by the lab. The adapter caps provider calls at 30
per MCP session; the lab enforces authoritative attempt quotas. Catalog prices
are planning inputs, never a substitute for provider billing receipts.
The complete provider socket response has a 125-second wait limit, shortened
by the remaining research time. Partial response reads do not reset that limit.

## Package and enable

```sh
python3 scripts/build_arena_bundle.py /tmp/tyche-codex-bundle
```

The builder stages an allowlist of source files, shared instructions/references,
taxonomy assets, the public catalog, `requirements.txt` and the license. It
excludes reports, local settings, credentials, tests and Git history. The only
Python package dependency is `geonamescache==3.0.2`, used for country/region
validation. Submit the staged directory through the existing lab source-bundle
and baseline promotion process; do not install the desktop launcher in the lab.

Before enabling a round, Leadpoet PR #198 needs passing required checks, then
merge and deployment of its Codex-equipped image and cost-reconciliation
migration `263-lab-arena-codex-cost-reconciliation.sql`. The migration-number
collision is resolved in PR #198; its SQL is unchanged.
The selected round must admit the model and install this source bundle's
dependency. Existing rounds retain their frozen baseline. This TYCHE PR does
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

## Boundary for the next Arena integration

TYCHE continues to own ICP interpretation, research decisions, evidence and contact
qualification, writing, and the native saved-run format. Its existing
`ResearchTools(execute=..., deliver=...)` callbacks are the provider and delivery
boundaries. The local launcher keeps its current Deepline/ScrapingDog setup,
workbook, preview and reports; the Arena adapter supplies the callbacks above.
Local use requires the existing Codex CLI and export runtime, not the desktop UI.

Arena should own model selection and API transport, provider credentials and HTTP
access, parallel workers, quotas, deadlines, cost reconciliation, checkpoint
storage and scoring. Its shared provider interface should accept ordinary
Deepline/ScrapingDog requests and return the original response plus billing and
request identity. Host model transport can serve OpenRouter-compatible requests
without moving qualification logic into the host.

This bundle still uses the current worker socket and pinned Codex host helper.
It does not add a generic any-model runner, ScrapingDog support in Arena, or a
second concurrency scheduler. Replace that adapter plumbing only when Arena's
shared interfaces are available and tested. Moving credentials, parallelism and
accounting into another TYCHE abstraction first would duplicate the host work.
