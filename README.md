# Leadpoet PydanticAI Harness

An open-source PydanticAI agent for live B2B company sourcing.

The harness takes one ICP and returns up to five companies that match it and
have a verified, recent intent signal. Each result includes a clear sales-facing
reason and public evidence URLs.

This repository contains no Research Lab deployment or persistence code. The
competition host calls the stable function directly.

## Development and promotion

- `main` is the development and testing branch. A push to `main` does not
  change the daily Research Lab baseline.
- `lab` is the explicitly promoted baseline. Research Lab downloads this
  branch when it freezes the baseline for a new rebenchmark.
- A running round keeps its downloaded source, including after a restart.
  Updating `lab` affects the next baseline snapshot, not saved results.

After testing a selected `main` commit, promote it with an ordinary
fast-forward push from a clean checkout:

```bash
git fetch origin
git switch main
git merge --ff-only origin/main
git push origin HEAD:lab
```

Do not force-push either branch. Promotion needs no release manifest,
attestation, or extra model-identity protocol. Miner forks still use the same
input/output contract and can use any harness.

## Stable contract

The public entrypoint is:

```python
from harness import run_icp

companies = run_icp(icp)
```

Its signature is:

```python
def run_icp(icp: dict) -> list[dict]:
    """Return up to five best-fit companies, ranked best first."""
```

The host passes one JSON object for one daily ICP. `icp_id` is always present
and non-empty. The current input fields are:

```json
{
  "icp_id": "icp_20260904_001",
  "prompt": "Find software companies with recent momentum.",
  "industry": "Software",
  "sub_industry": "SaaS",
  "geography": "United States",
  "country": "United States",
  "employee_count": ["51-200", "201-500"],
  "company_stage": "Series A",
  "product_service": "A business platform used by operating teams",
  "required_attribute": "Sells a business product used by operating teams",
  "intent_signals": ["Announced a funding round in the last 12 months"],
  "intent_signal": "Announced a funding round in the last 12 months",
  "intent_category": "FUNDING",
  "intent_max_age_days": 365,
  "bonus_intents": []
}
```

An agent must accept additional input fields so the host can add descriptive
ICP data without changing the function signature.

The current primary intent is at index 0, with its category and freshness in
`intent_category` and `intent_max_age_days`. Bonus intents are optional and
never replace the primary intent. Structured `required_intents` are also
accepted for standalone callers. `product_service` describes the target
company's own offering; it is a fit criterion, not evidence of buying intent.

The return value is a JSON list, or `[]` when no company can be verified:

```json
[{
  "company_name": "Example",
  "company_website": "https://example.com/",
  "company_linkedin": "https://www.linkedin.com/company/example/",
  "industry": "Software",
  "employee_count": "51-200",
  "company_stage": "Series A",
  "country": "United States",
  "state": "California",
  "fit_summary": "Why the company fits the ICP.",
  "fit_evidence_urls": ["https://example.com/about"],
  "intent_signals": [{
    "matched_icp_signal": 0,
    "description": "The required recent event.",
    "date": "2026-08-20",
    "why_now": "Why a sales representative should contact the company now.",
    "url": "https://example.com/news/event",
    "snippet": "Source text that supports the claim."
  }],
  "required_attribute": {
    "text": "The required company characteristic.",
    "passed": true,
    "evidence_url": "https://example.com/about",
    "evidence_quote": "Source text that proves the characteristic.",
    "explanation": "Why the evidence satisfies the requirement."
  }
}]
```

The harness can use these tools: `search_companies`, `get_company_profile`,
`get_company_events`, `search_web`, `fetch_page`, and `submit_companies`.
For `HIRING` or `JOBS` event lookups, `get_company_events` accepts one optional
PredictLeads `job_category` and returns a bounded plain-text job description
when the provider supplies one.
In the Arena, reasoning uses OpenRouter. Web search uses ScrapingDog first,
with Exa through Deepline as a fallback. Page retrieval uses Exa first, with
ScrapingDog as a fallback. Profiles, events and contacts use Deepline.
`get_company_profile` accepts an optional source-discovered `company_linkedin`
to avoid a redundant lookup. Funding research is an explicit event lookup.
The harness uses the two separate 30-call provider
allowances and reserves one search plus one profile/email call per requested
contact. Fallbacks can need additional calls within the same limits.
The host still enforces time and cost limits. Provider credentials
stay on the host. The standalone tools below remain separate.
Arena research uses the metered dollar budget instead of an additional
cumulative input-token cutoff. Request, tool, output-token, and time limits
still apply. Page retrieval rejects binary files presented as HTML.

### Contacts

When an ICP includes `"contact_policy": "contacts_v1"`, the model can check
`get_company_contact` during research, before spending more calls on a candidate.
Each company's bounded lookup is reused at final submission, including misses.
Companies without an earlier lookup still receive the existing final contact
search. Early lookups spend the same reserved call capacity and keep the same
host limits. The ICP supplies
`target_roles`, optional `target_seniority`, and `contact_geography` with
`countries`, `regions`, and `cities` lists. Contact location is separate from
company headquarters.
Exact requested titles are checked before broader title matches, within the
same three-profile limit. Full-profile identity and role checks still apply.

Each supported contact is added to its company in this form (example only):

```json
{
  "contact": {
    "full_name": "Jane Doe",
    "role": "Chief Technology Officer",
    "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
    "location": {"country": "US", "region": "California"},
    "email": "jane@example.com",
    "email_source": {
      "provider": "harvestapi",
      "tool": "harvestapi_get_profile",
      "record_id": "provider-profile-id"
    }
  }
}
```

Discovery uses HarvestAPI through Deepline, with one search page and at most
three profile lookups per company, within the existing time and call limits.
Names, current roles, locations, and emails come from provider records, not
guesses. The subnet independently checks identity, employer, role, location,
and email. Valid and explicitly labeled catch-all emails can qualify.

If no supported contact is found, the company remains in the output without
`contact`; it receives no credit in a contact-required round. Contacts are
omitted for company-only ICPs. A successful sourcing run is not a guarantee
that every company or contact will pass independent scoring.

## Miner competition contract

Miners can fork this repository and change the model, harness, prompts,
routing, tools, and dependencies. Submit the source folder through the miner
menu. The subnet requires only a top-level `harness.py` with this callable:

```python
def run_icp(icp: dict) -> list[dict]:
    """Return up to five companies, ranked best first."""
```

The host passes one ordinary ICP dictionary and validates the returned list
against the output fields shown above. It also supplies approved provider API
access and enforces the same time, call, and cost limits for the baseline and
all miner submissions. The CLI sends runtime credentials separately from the
source archive; the sandbox receives provider access, not raw provider keys.

The source folder can contain normal local modules and a `requirements.txt`.
It does not need a Dockerfile, command-line adapter, Git commit, source
identity, receipt, manifest, replay proof, or GitHub attestation. The upload
checksum is used only to detect a damaged transfer.

Submit this source repository to the Arena as the public baseline or use it as
the starting point for a miner fork. The host imports `harness.run_icp` and
supplies provider access through its worker socket.

## Install

Use Python 3.11 or newer and Node.js 20 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-local.txt
npm ci --prefix experiments/harness_bakeoff/deepline
export BAKEOFF_DEEPLINE_BIN="$PWD/experiments/harness_bakeoff/deepline/node_modules/.bin/deepline"
```

For the local runner only, set `OPENROUTER_API_KEY`, `DEEPLINE_API_KEY`, and
`SCRAPINGDOG_API_KEY` in the process environment. `EXA_API_KEY` is optional.
The Arena adapter does not read these keys. Do not commit keys or private ICP
data.

## Run live sourcing

Store real ICPs in an uncommitted JSON file outside this repository. The file
can contain a JSON array or an object with an `icps` array.

```bash
python -m experiments.harness_bakeoff.runner preflight
python -m experiments.harness_bakeoff.runner all \
  --icp-file /absolute/path/to/icps.json \
  --evaluation-date YYYY-MM-DD
```

The smoke phase runs one live one-company attempt. The scored phase runs each
selected ICP twice. Each attempt uses a fresh process and the same provider,
token, time, and cost limits. Results must be written outside the repository.

## Optional standalone one-shot adapter

The native Arena imports `harness.run_icp` directly and does not use
`production_runner.py`. This optional adapter prints one `PYDANTIC_HARNESS_RESULT_JSON=` line for
reliable parsing. Run its live preflight once per daily batch. Pass the selected
model and its public pricing to each run; `run` does not repeat the paid model
probe.

```bash
python production_runner.py preflight
python production_runner.py run \
  --model openai/example \
  --model-pricing-json '{"prompt":"0.000001","completion":"0.000002"}' \
  --evaluation-date YYYY-MM-DD < /absolute/path/to/one-icp.json
```

Each `run` call reads one raw ICP JSON object and starts a fresh `run_icp`
worker process. The adapter does not deploy code or persist state.

## License

MIT
