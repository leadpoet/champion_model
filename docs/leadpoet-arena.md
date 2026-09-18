# TYCHE in the Leadpoet lab

See [the compatibility audit](leadpoet-codex-audit.md) for historical protocol
findings. Offline delivery tests alone do not establish compatibility with a
deployed broker or guarantee sourcing quality. Production validation must follow
the complete round through execution, scoring, settlement and publication.
GitHub Actions results are optional diagnostic evidence, never authority to
start a canonical restart or rebenchmark.

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
  → native TYCHE MCP tools → lab worker → Deepline or ScrapingDog
  → review each completed company → validate → atomic JSON checkpoint
  → continue research → final delivery or deadline
  → revalidate the last published snapshot → return companies to the lab
```

The adapter calls the host's `session(model=..., reasoning_effort=...,
request_guard=...)`, adds
the TYCHE MCP configuration to that session's isolated `CODEX_HOME`, and runs
one Codex process while the session remains open. PR #198 owns the Responses
bridge and sends `openrouter.responses` through the lab worker. TYCHE never
implements or replaces that model transport.

TYCHE's research tools use its shared MCP transport. A lab-only
`tyche_checkpoint` compatibility tool remains for existing callers. The host initializes the request, so `tyche_start` is unavailable
to the model. The lab already isolates the process in gVisor; the adapter does
not invoke the desktop launcher's nested sandbox relay. Shared qualification,
email, accounting, stopping and final evidence-review gates still apply.
The lab delivers reviewed JSON and can save completed companies before the
whole run is ready to stop. The desktop finish path still requires its strict
whole-run preflight and workbook delivery.

The default is `openai/gpt-5.6-luna` with `xhigh` reasoning, matching the local
launcher's model family and effort. The round must include that model in its
price table and support it through OpenRouter Responses. Validate admission
against the deployed round and broker. The local launcher's Fast setting is omitted:
PR #198's closed request schema does not accept `service_tier`. There is no
automatic fallback to another model or personal Codex login.
Native Codex model metadata and code-mode behavior are retained. Explicit
`agents.enabled=false` and `features.multi_agent_v2=false` keep this a single
research worker; `features.multi_agent=false` alone does not override Luna's
model metadata. Image generation is disabled for this text-only workflow.

The bundle refuses execution outside `/agent/source` or without the lab's
two socket mounts, host-mounted runtime helpers, executable and output path.
It is for new parallel-execution lab rounds, not historical or local runs.
The research deadline is 2,070 seconds; the Codex process is bounded at 2,670
seconds, reserving the latest native TYCHE launcher's ten minutes for final
review and export inside the lab's 2,700-second window. The remaining 30 seconds
belong to the host's signed cutoff and response handling. The outer signed
deadline and quotas always remain authoritative.
The runtime session must support `wait_idle(timeout_seconds)`. Before starting
another Codex invocation, the model waits for any previously dispatched model
request to settle. This passive wait does not send, cancel or replay a provider
call, and uses the same phase and response deadlines. If the finalization wait times out,
the model preserves its last reviewed checkpoint without starting a finalizer.
Between invocations, the adapter uses native saved-dispatch recovery. A complete
saved response can restore its missing route without another provider call;
unresolved accounting still blocks continuation. Finalization requests the
combined evidence packet with `tyche_finish` before inspecting individual fields,
following the native launcher. Reviewed JSON replaces workbook export in Arena.
The runtime must also expose the passive per-run quota snapshot and the
pre-dispatch request guard. TYCHE checks a fresh snapshot before each Responses
dispatch, stops admitting more research with forty OpenRouter identities left,
and admits finalization only while capacity remains. Native contract reads,
evidence paging and review can need more than nineteen turns. One last admitted
research dispatch can use up to twelve identities through host retries, leaving
at least twenty-nine identities for finalization. The guard also applies a
one-second freshness barrier so each serial admission sees the authoritative
post-dispatch ledger rather than a cached snapshot. This is operating headroom,
not a guarantee against provider failures during
finalization; Arena's existing quota and ledger stay authoritative.
TYCHE creates its local saved run before the first passive snapshot so both use
the same original research deadline. That initialization reads only the bundled
catalog. Codex and paid research remain blocked until the snapshot succeeds.

An already admitted model or MCP request may settle after the research deadline,
up to the 2,670-second response bound. New research Responses and new paid MCP
lookups are refused at 2,070 seconds. After the admitted work drains, TYCHE uses
its saved state and unchanged native stop decision for finalization. If quota
headroom closes earlier while native progress still says `continue`, the adapter
waits for the original research deadline. It does not invent target completion,
change the saved budget, or publish an early empty result.
Timeout/error paths close the
session, kill the process group and save bounded diagnostics. The MCP process
also watches its Codex parent because Codex gives MCP a separate process group.
They never relaunch a potentially billed call or silently deliver unfinished records.

## Growing JSON and partial completion at cost or time limits

After accepting each company, `tyche_review` returns its source packet. Codex
reviews it and approves the current `review_ref` through `tyche_review`. Native
TYCHE saves `leads.json`; the adapter maps only confirmed rows to Arena's v5
schema and publishes `/output/companies.json` through the host's atomic checkpoint
writer. No separate `tyche_checkpoint` call is required. That tool remains
compatible for existing callers. Qualification, original sources, contacts,
email provenance, accounting, and Arena projection checks remain enforced. The
original target and budget stay unchanged.

The files grow from one confirmed company to two and onward while research
continues. At a provider or model cost cutoff, ICP deadline, or worker failure,
the last successfully published list is already available. A failed publication
blocks the next paid lookup or unrelated page read until publication succeeds.
Retry or MCP restart reuses the approved JSON without repeating paid calls.
Changed or withdrawn leads are removed until reviewed again. Exact saved-source
corroboration remains available during a pending repair.

TYCHE stores the published research snapshot and its approval separately. An unfinished next
candidate, later uncertain billing, process timeout or model error does not erase
the earlier checkpoint. On orderly process shutdown, the harness revalidates that
snapshot's saved evidence and requires the local and host output to match it.
A failed checkpoint write leaves the preceding saved checkpoint intact.

For rounds using `atomic_checkpoint_45m_v1`, Arena's runtime keeps the last valid
checkpoint it completely read **before** the signed deadline, including when it
kills the sandbox. It does not recover unsaved drafts or output written after the
deadline. Two completed, reviewed companies out of a target of five can therefore
enter normal scoring; the remaining three are unfulfilled. Factual qualification,
contact provenance, duplicates and the round's scoring policy still determine
credit. TYCHE's ordinary finish path is still available to close a completed run.

## Input and output

- Supports `intent_details_v1` and `contacts_v1`, with the lab's v5 company
  output and `LAB_ARENA_COMPANY_LIMIT` of 1–5.
- Keeps the original ICP, roles, exclusions, company criteria, required
  attribute, contact geography and seniority. Primary signals and required
  attributes must be text. The primary signal at index 0 is mandatory.
  Generated `bonus_intents` remain optional, preserve scoring order, and use
  their individual age limits.
- Contact email must appear in the selected HarvestAPI `get_profile` receipt
  requested with `findEmail: "true"`. Its actual provider record ID is emitted
  as `contact.email_source.record_id`. Local route IDs and Deepline request IDs
  are not substituted for lab broker call IDs. The lab's verifier remains the
  authority on factual fit and provenance.
- `tyche_finish` first produces the existing evidence packet. Approval of its
  current `review_ref` runs strict validation, maps only accepted records, saves
  `companies.json` and `validation.json`, then calls `lab_arena_checkpoint.write`.
  Delivered state is closed to further research changes.
- After Codex exits or its local timeout fires, the harness validates the last
  published snapshot against the original ICP and requires identical saved and
  checkpointed JSON. It returns
  the company list for the lab's normal entrypoint. Final text alone is never
  delivery. Checkpoints contain only reviewed output; the adapter does not
  periodically publish unreviewed drafts.

## Provider boundary

Provider research uses the lab's existing `deepline.execute` and closed
ScrapingDog operations. The bundled public catalog supplies Deepline metadata
locally; no Deepline CLI installation is needed inside the lab. The adapter
maps the supported native ScrapingDog routes to existing Arena operations and
keeps native response normalization. Unsupported routes and options fail before
dispatch. Hosted web search and manually injected web observations remain
unavailable. There is no direct-provider fallback.

`tyche_open` performs one bounded GET through the host proxy and saves its own
native receipt. It does not follow redirects. For HTTP 301, 302, 303, 307 or
308, it exposes only a validated public `Location` target and, during research,
an explicit next `tyche_open` action. That second call passes through the normal
URL, proxy, deadline and receipt checks. Finalization can still reopen only an
exact source URL already saved for the accepted company and does not offer a
new redirect action.

The Arena frames for `google_search`, `google_news` and `google_jobs` accept
`query` plus optional `country`. Search and news use the host-fixed result count
of 10. Other native options are rejected before dispatch rather than removed
from a provider request.

Raw provider receipts, billing, identities and local reservations are retained.
Uncertain transport keeps its reservation and blocks further calls to that
provider. Successful ScrapingDog calls retain their native estimated reservation;
Arena accounts for their actual cost through its existing pricing contract.
Both providers share the unchanged USD 0.50 allowance per requested company,
including the email-verification reserve. Model costs are separate and enforced
by the lab. ScrapingDog is enabled only when Arena supplies its public runtime
handle, at the existing Arena rate of USD 0.00005 per credit. Its credit
allocation does not add dollars to the shared cap. The adapter rejects a call
bound below Arena's existing operation cost instead of changing the bound.
Local dispatch counts and uncertain outcomes survive MCP continuations; each
provider is capped at 30 calls per attempt. Arena quotas and billing remain
authoritative. The native provider timeout travels in the operation frame.
Deepline execute is capped at 240 seconds and its response envelope can use up
to 305 seconds for Arena admission, execution, billing and API grace.
ScrapingDog remains capped at 60 seconds with a 125-second envelope. Both waits
remain bounded by the original response deadline, and partial reads do not
reset that deadline.

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

Before enabling a round, verify the deployed Codex-equipped image, runtime
contract and required cost-reconciliation schema through the canonical local
controllers. Preserve their source, signature, PCR0, migration and readiness
checks. GitHub attestation and CI test completion are not restart or rebenchmark
dependencies.
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
# Exercise the adapter against a checkout of the deployed Arena contracts.
LAB_ARENA_REFERENCE_SOURCE=/path/to/leadpoet python -m pytest tests/test_arena_codex.py -q
python -m unittest discover -s .agents/skills/lead-sourcing/tests -p test_research_tools.py
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
These tests use fixture processes, checkpoints and provider replies. The optional native
tests run the actual Codex CLI and code-mode companion with scripted loopback
responses: two MCP calls, continuation and forced context compaction. A separate
case records the original PR #198 contract rejection as an expected failure.
That historical fixture does not validate the updated upstream protocol.

The optional Arena-source tests exercise real native run initialization, MCP
tools, reservations, saved receipts and framed worker transport with scripted
responses. They also compare operation schemas and prices against Arena's
production implementations. They do not make paid provider calls.

Each changed candidate still needs deployed Codex-to-MCP and live provider
validation, including sourcing quality and the full round result. A lab smoke run
is required after the protocol fixes before promotion; unit checks cannot prove
that deployed journey. It was not run as part of this code-only integration.
