# Test TYCHE without global Codex instructions

## Ordinary requests in Codex

The repository's `AGENTS.md` routes actual lead/company sourcing requests to
the isolated launcher. In a new Codex conversation opened in this repository,
you can say "Source 10 leads for [ICP]". You do not need to mention isolation.
Code changes, questions, and reviews of existing results stay in the outer
conversation. "Change X, then source Y" runs the code change first and then
starts the isolated sourcing test.

This is instruction-based routing by Codex, not a hard-coded keyword filter or
an application hook. Existing conversations need to read the new `AGENTS.md`
or be restarted to pick up the rule. It does not apply outside this repository.
The isolated child receives `TYCHE_ISOLATED_RUN=1`; its instructions and the
launcher both prevent recursive launches.

The outer agent writes the request and relevant user constraints to a run's
`request.txt`, then calls `--exec-file`. It passes task context, not global
instructions or the whole conversation. The outer agent reviews saved outputs
before reporting success. Continuations must retain the same ledger and
remaining budget; a fresh rerun is a separate billable sourcing run.

For explicit exclusions, the outer agent also writes `request-exclusions.json`
beside `request.txt`, as a UTF-8 JSON array of all user-supplied exclusion names
and categories. This complete list replaces the model's `icp.exclusions` during
startup; the model may omit that field. It must not include research candidates
or contrary findings that the user did not exclude. Invalid files fail before
catalog calls or ledger creation. The exact list is saved in the request, and a
different sidecar cannot change an existing run's exclusions on resume. Runs
without this file retain the normal interpreted-request path.

## Host-terminal execution

Launch the wrapper from the host terminal. In Codex, use the terminal tool's
`sandbox_permissions: "require_escalated"` option when available, with this
repository as `workdir`, and follow normal approval review. Use this for the
first sourcing launch, continuations, and setup checks. The wrapper does not
request escalation itself; the outer agent chooses the terminal tool settings.
When running manually, start the command in your regular terminal.

On 2026-09-11, launching inside an outer Codex command sandbox failed with
`reserve managed loopback proxy listeners`. The same isolated launcher started
successfully from the approved host terminal, retaining its own workspace-write
sandbox and network proxy. Host execution preserves the separate temporary
profile and local-only instructions/skills. It does not require network
allowlist edits or sandbox-bypass flags.

If the current tool cannot request host execution, or approval review denies it,
report the restriction and retain the saved request. Do not route around that
decision through another tool or silently switch to sourcing in the outer chat.

## Manual launch

Keep developing normally in the Codex desktop app. Launch a separate, fresh
Codex CLI test against this same checkout:

```bash
python3 scripts/codex_tyche.py
```

Each launch creates a private temporary Codex profile. It reuses the existing
file-based Codex login and execution-policy rules, but does not import global
AGENTS.md, user configuration, plugins, apps, or memories. It discovers skills
through Codex itself, disables every skill outside this repository's
`.agents/skills`, and checks the actual loaded instruction sources before
starting. Repository instructions and `.codex/config.toml` still apply. The
launcher pins `gpt-5.6-luna` with `high` reasoning and the `fast`
service tier (the accelerated 1.5× mode when the account exposes it).

The child disables Deepline CLI self-updates and global skill synchronization
using `DEEPLINE_NO_AUTO_UPDATE=1` and `DEEPLINE_SKIP_SKILLS_SYNC=1`. This keeps
research on the installed runtime without npm downloads during provider calls.
CLI compatibility checks remain enabled; install any required CLI update
through normal host setup before starting a new run.

Your global configuration is not edited. Codex's built-in system instructions
and managed permissions remain in force. This isolates supplied context; it is
not a filesystem security boundary preventing all possible external reads.

## Native research tools

File-backed runs register six local tools only in the temporary profile:
`tyche_start`, `tyche_claim`, `tyche_lookup`, `tyche_review`, `tyche_inspect`, `tyche_finish`.
The run file comes from the launcher's request-file directory, not model input.
Provider credentials and bundled runtime paths are forwarded as environment
variables; values are never copied into the temporary config or prompt.
The launcher also supplies its start timestamp, so native run timing includes
initialization and setup. Resuming an existing run keeps its original clock.

Free prerequisite catalog reads share a 120-second startup window, shortened by
the remaining user deadline. A transient timeout or provider error gets one
retry after two seconds: the first attempt allows 30 seconds, the retry 60.
Successful descriptions are reused; authentication, quota and schema failures
stop immediately. Each catalog receipt retains the attempt number, start time,
elapsed time, timeout and original response/error. This does not retry paid calls
or extend research time. The Arena adapter uses its bundled local catalog.

The stdio relay advertises Codex's `codex/sandbox-state-meta` capability. On the
first tool call it starts one child through `codex sandbox --sandbox-state-json`
using that exact caller metadata. This matters: a standalone workspace sandbox
does not by itself reproduce the worker's managed network proxy. Missing or
changed metadata fails before further research. The project network allowlist
and worker settings remain unchanged. `--check` verifies discovery; `--smoke`
also exercises the sandboxed read-only tool call without sourcing providers.

The child uses the existing helpers, one ledger and three provider dispatch
slots shared by native calls. Research decisions and source exhaustion remain
explicit LLM inputs. Built-in web search stays available separately; one review
call saves its observed evidence alongside findings, without a plan-file cycle.

Closing the connection cancels queued requests and lets dispatched work save
receipts where possible. Forced process termination can still leave an uncertain
provider outcome; retain its pending charge and reconcile instead of retrying.
No daemon survives intentionally between runs. Legacy interactive/`--exec`
sessions keep the CLI helper path because they do not supply a bound run file.

## Parallel company research

`--exec-file` defaults to two researchers. `--workers 1` preserves the single
researcher mode for comparison; `--workers 3` is also supported. Each researcher
runs the same discovery → company qualification → contact enrichment loop, with
different starting search approaches. The first worker initializes the ICP once;
the others start after its setup receipts and shared ledger are saved.

Code manages spending; researchers continue their normal company workflow while
calls are eligible. At 80% of the saved budget, posted billing is reconciled and,
if still near the limit, the pool switches to one researcher. Other workers
finish their current company and then receive `worker_yield`; they end without
polling or opening another company. The original configured worker count and
claims remain intact across resumes. Pacing never releases uncertain charges or
increases a cap. When settlement lowers exposure below 70%, healthy workers
resume; the gap prevents repeated switching around the threshold. A hard cap
can still stop a company before completion.

A run has one OS-locked supervisor. Continuations refuse live saved process groups
and close stale worker state only after those groups exit. Launcher output is
saved in `launcher.log`; a disconnected terminal does not break output capture. State
writes use automatically released OS locks and atomic replacement. Legacy `.lock`
files still require verified recovery. Full local validation allows 120 seconds
per stage, within the existing finalization allowance; research clocks stay fixed.

The default accounting remains main's observed provider-plus-model cutoff.
For a like-for-like historical provider-cap comparison, launch a **new** run with
`--budget-policy reserved`; it retains the existing hard provider reservations
and automatic email-verification reserve, with model use reported separately.
A saved run cannot switch accounting policy. These are different cost contracts;
always report which one was tested.

Each researcher keeps one `current_company` in the existing worker registry.
It follows that company through qualification, contact enrichment and confirmed
lead review before claiming another or running broad discovery. An evidenced
rejection or explicit `hold_account`/`hold_contact` review also clears the slot;
the hold must explain the missing evidence and why available routes cannot
resolve it. Held companies retain their owner and evidence. A later lookup
resumes that company only when the worker's slot is free. Restarts retain the
current company. Company-scoped searches remain available for follow-up.

`tyche_claim` atomically reserves a domain and its known LinkedIn company identity.
A LinkedIn-only candidate needs its website domain from discovery first, so the
existing domain-based pipeline retains one target throughout. Known
aliases share the claim; provider company URLs and reviewed getter identities
extend it. Rediscovering a candidate is possible, but another worker cannot
research or review a claimed company. Unrecognized alternate identities cannot
be deduplicated until linked; identity conflicts are refused when detected.
Claims persist in `results.json.workers.json` through restarts. A replacement
invocation retains its worker slot and companies only after the previous
invocation has exited. There is no timed ownership expiry or blind paid retry.

One local coordination module serializes short writes to existing run/ledger
files using OS locks. Network calls run outside the state lock. Three provider
slots are shared across all researchers, rather than multiplied per worker.
The existing spend reservation and evidence gates still apply. Persistent
`.tyche-*.guard` files are lock handles, not unfinished transactions; never
delete them during a run. Existing fail-closed `.lock` files retain their
original recovery semantics. A lead-count change between planning and reservation
returns a proven-unsent response that can be replanned without charging or
replaying an uncertain call.

Each worker reviews and confirms only its own leads. Incremental `leads.json`
publication preserves other workers' confirmed rows under the same shared lock;
another worker's pending evidence review does not pause unrelated research.

The supervisor stops new research at the shared target, budget, or deadline,
waits for researchers to exit, reconciles saved dispatches, and then uses the
existing single final-review/export path. A worker-specific failure retries only that worker, retaining its company and
receipts. Repeated local failures disable that slot while healthy peers continue;
shared account, receipt/registry accounting, invalid-state and cleanup failures still stop the
pool. Incomplete usage counts as a failed invocation, and disabled slots remain
disabled on resume. If initial setup never creates an authoritative run, no
automatic model retry is made. No unfinished paid call is blindly replayed. Cancellation terminates only the pool's
owned process groups. Uncertain paid outcomes keep their reservations.

Final-review continuations read the current saved request and evidence. New
operator feedback appears on its first invocation only; later finalizers must
reassess current receipts instead of repeatedly applying an old verdict.

A free description refresh of an already used tool remains eligible after the
research deadline. The supervisor can refresh a mandatory service with a saved
authentication/quota failure once on resume, then finalize if access is restored.
This does not authorize new discovery, paid execution, receipt rewriting or a
clock extension. If recovery fails, the original blocker remains visible.

Each invocation saves its own `model-usage` receipt and `worker-logs` transcript.
The aggregate cost report includes every researcher and final reviewer; provider
caps do not cap model subscription usage or constitute an actual model invoice.
Assess concurrency, elapsed time, unique reviewed companies, prevented duplicate
claims and strictly accepted leads together.

The launcher uses the documented [Codex non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode)
and [MCP configuration](https://learn.chatgpt.com/docs/mcp). Worker sandboxes,
network policy, temporary-profile isolation and model settings remain in force.

## Checks

First activate the Python environment and install the pinned requirements as
described in [Quick start](../README.md#1-prepare-your-environment).

Check isolation and initialize a session with the same project sandbox and
network settings used for sourcing, including its network proxy. Run this from
the host terminal; it starts no model turn and makes no sourcing-provider calls:

```bash
python3 scripts/codex_tyche.py --check
```

After the same startup check, run one read-only model smoke test that reads the
local skill and reports its deliverables, without sourcing or provider calls:

```bash
python3 scripts/codex_tyche.py --smoke
```

`--check` verifies session initialization, not Python dependencies, model-service
connectivity or provider credentials. `--smoke` additionally verifies a model
response; its model turn runs read-only with command networking disabled.

Run a supplied request without the terminal UI:

```bash
python3 scripts/codex_tyche.py --exec 'Use $lead-sourcing. <request and explicit budget>'
```

For a multiline request saved in a file:

```bash
python3 scripts/codex_tyche.py --exec-file reports/<run-id>/request.txt
```

Load provider environment variables in the launching shell as described in
README.md. This launcher does not source `.env` automatically. Paid sourcing
still requires the existing budgets and adapters. With a ChatGPT Codex login,
model calls use that login's allowance; provider charges remain separate.

For `--exec-file`, the launcher saves `model-usage/<invocation-id>.json` beside
the request file. It reads per-response usage from the worker's session journal
inside the existing temporary profile and reconciles it with the final CLI JSON
totals. Only model/request identities, timestamps, numeric input, cached input,
cache writes, output and reasoning output, and dated pricing are retained in
the receipt. Prompts and tool output are not copied into it. Pricing is applied
per response so cumulative input does not accidentally trigger long-context
rates. Reasoning tokens are already included in output and are not billed twice.

Each continuation gets its own receipt. Failed or interrupted invocations retain
observed usage but remain incomplete when final totals cannot be reconciled.
Capture failures mark cost incomplete and pause new paid work; the launcher
exits nonzero and preserves the observed subtotal. This does not invalidate saved leads or
authorize rerunning paid calls.

The launcher automatically writes `run-costs.json` from `results.json` and every
invocation receipt before deleting the temporary profile. Missing results or
usage remain explicitly incomplete, never free. The launcher uses a temporary
session journal for this path instead of `--ephemeral`; sandbox, network, local
context isolation and cleanup are unchanged. Other execution modes remain
ephemeral. An already running invocation cannot gain retrospective capture.
The old `tokens used` footer excludes cached input; it is not a pricing breakdown.

To recalculate the report after provider billing is reconciled:

```bash
python3 scripts/run_costs.py reports/<run-id>/results.json
```

The scope is only the TYCHE run: provider calls and sourcing model responses,
including retries, continuations and compaction. Outer chat and development are
excluded. The report has one known `total_usd`, with `provider_usd`,
`estimated_llm_usd` and `pending_provider_calls`. Missing usage or billing makes
status `incomplete`; it never creates a projected maximum or a free call.

Base LLM estimates apply current recorded model rates to individual responses,
including input, cache reads/writes and output. They exclude Fast premiums,
hosted tools and subscription allocation. This is not an actual invoice.

New version 2 ledgers apply the combined soft cutoff during execution. The
launcher polls this worker's usage journal while it runs, including silent
periods, and stops on observed exhaustion. It allows dispatched provider calls
to save their responses first. Polling and already-running requests can cause
overshoot; the cutoff is not a guaranteed spending ceiling. No further model
finalizer starts after exhaustion. Already reviewed leads remain in `leads.json`;
drafts are not promoted to delivery. An interrupted model response may leave
usage incomplete, which is reported and prevents automatic continuation.
Before exiting for budget exhaustion, a local checkpoint saves the derived stop
reason and frontier audit through the existing strict preflight. Its validation
findings are included in `worker-status.json` as `stop_validation`; remaining
sources stay unreviewed and failed checks stay visible. This checkpoint never
approves evidence, exports a final workbook, or starts another model turn.

Missing provider billing pauses new provider calls. The current model response
can finish normally so its usage is retained; the combined cutoff stays active.
Before launching any continuation, read-only reconciliation uses exact saved
request IDs, posted/free billing and bounded pagination. Timeouts do not replay
research. See [billing-only recovery](../.agents/skills/lead-sourcing/references/provider-pricing.md).
Historical ledgers retain their original caps and reservation semantics.

Final review approval is bound to the current research and source-review state.
The launcher can retry deterministic export once after an interrupted finish
only when that exact state was already reviewed. It verifies the saved results
and workbook hashes, reruns the strict delivery gate, and writes `worker-status.json`.
An early worker exit can start another isolated invocation on the same saved
request, clock, ledger and receipts only while budget and usage accounting permit it. The launcher does not approve evidence
or retry provider calls. Each invocation retains its own usage receipt.
If a worker exits before initializing the run, the launcher stops without an
automatic retry. Repair startup before explicitly resuming the saved request;
its original clock and captured model usage remain intact.
Initialization has a separate ten-minute watchdog until `results.json` exists.
A hung startup records `startup_timeout`, preserves usage and does not retry.
This watchdog ends when the run initializes; it is not a research deadline.

New requests have no research deadline unless the user specifies one. Budget,
usage, cancellation and failure safeguards remain active. Resuming preserves any
saved deadline and the original start. Older saved requests retain their existing
limits. A watchdog terminates the worker's process group at an explicit saved
deadline even if it is silent.
In-flight charges remain uncertain until their saved responses or billing
can reconcile them; killing a local process does not cancel remote charges.

When the user explicitly asks to continue after that window, the outer operator
may supply `--resume-until <timezone-aware ISO timestamp>` and
`--resume-reason <user authorization>` with `--exec-file`. Record a bounded new
deadline; do not infer unlimited time. The launcher appends an audited extension
without changing the request, original start, ledger, receipts or spending caps.
Repeating the same timestamp and authorization is idempotent. This is not a
worker tool or an automatic extension. On this explicit resume, a saved mandatory
provider quota/auth failure permits one recovery invocation to refresh the free
tool description. Paid calls remain blocked until that succeeds, and previously
attempted paid requests remain protected from redispatch.

Once mechanically ready, research returns `review_handoff` instead of approving
its own final packet. The existing supervisor starts a fresh finalization context
with the same model, request, evidence and ledger. Standalone tool callers retain
their existing review path. This separates final review from the research history;
it does not establish semantic correctness without a successful live test.
After target or budget, that finalization-only invocation may use the original
remaining time to review saved prose/evidence and export. At the deadline, allow
a ten-minute finalization grace for model startup, evidence review and workbook
rendering. Provider dispatch and web search are disabled
in that invocation; this grace never extends sourcing.
The same final-review and workbook gates apply to complete and partial results.
`worker-status.json` reports `artifact_verified` separately from `target_met`; a
verified shortfall has status `partial`, its accepted/target counts and shortfall.
Only verified output reaching the requested count has status `complete`.
Continuations retain the invocation's specific review feedback. If review demotes
a lead below the target, the supervisor re-evaluates the saved budget and original
deadline and resumes research when allowed; it never resets either limit.
Delivery requires an export completed during the current invocation. An interrupted
repair cannot deliver an older workbook; automatic export recovery likewise requires
a review recorded during the current invocation, not an earlier approval.
A model usage limit, denied access, invalid state or two consecutive worker failures
is a runtime blocker, never a successful
research outcome. Cancellation does not restart the worker. No retry-count limit
is applied to substantive research attempts; useful work continues to its limits.

The temporary profile and its session history are removed when the launcher
exits. Files saved in the project, including sourcing results and receipts,
remain. Start a fresh launch after editing the skill to avoid stale context.
This is a local test workflow, not the production job/recovery host.

The launcher uses the installed Codex app-server's discovery protocol. It was
checked with Codex CLI 0.154.0-alpha.6.2 and fails closed if instruction-source reporting
or skill discovery is unavailable. Existing desktop conversations already
contain their earlier context; this launcher does not clean or modify them.

## Workbook finalization and usage reconciliation

The launcher supplies `TYCHE_WORKSPACE_NODE`, `TYCHE_WORKSPACE_NODE_MODULES`
and `TYCHE_WORKSPACE_PYTHON` from the installed desktop bundle when available,
preserving explicit environment overrides. Other hosts configure these paths
once; runtime dependencies are never downloaded during research. Finalize saved
reviewed results with:

```bash
node .agents/skills/lead-sourcing/scripts/export_xlsx.mjs reports/<run-id>/results.json
```

It uses full strict validation, verifies the
exported workbook's lead/source values, and saves validation, inspection and PNG
preview files beside the workbook. The preview still requires visual review.

State writes use a persistent `.write.lock` file with operating-system ownership,
which releases when the writer exits, including forced termination. Never delete
that file while a run may have writers. While held, a hardlinked `.lock` sentinel
also excludes older writers. After a crash, the next OS-lock owner can reuse that
same-inode sentinel. An unrelated legacy `.lock` remains a blocker until its
original owner is verified stopped; the runtime never guesses from its age.
Exporter subprocess timeouts preserve the failed stage and original error. A
reaped child with available saved state can retry export without research; an
outer exporter timeout or unavailable state remains an export failure until
process exit and state are verified.

Model receipts retain numeric usage, response identities and explicit
`compacted.compaction_response_id` linkage from the isolated worker journal.
If the CLI excludes linked compaction responses, reconciliation compares the
ordinary responses to its total while pricing **all** captured responses. No
response is excluded based on a guessed token difference. Missing linkage,
missing usage, or unexplained differences remain incomplete. Private compaction
messages and replacement histories are not retained. API-equivalent estimates
remain distinct from actual model billing.

## Shared Arena runner and version

The local launcher and Arena `run_icp` call the same supervisor. Install Codex
0.154.0 with `npm install --prefix .runtime --no-audit --no-fund --save-exact @openai/codex@0.154.0`.
The launcher selects that repository-local executable, or `TYCHE_CODEX_BINARY`,
and rejects version drift before a model turn. This preserves the user's global
Codex installation. Arena supplies its pinned binary through its existing host image.
