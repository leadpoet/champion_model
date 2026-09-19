#!/usr/bin/env python3
"""Persist numeric Codex usage and report complete or explicitly partial run costs."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import sys
import subprocess
import threading
import time
import uuid

# Standard API-equivalent USD per million tokens, checked 2026-09-13.
# This is a comparison estimate, never a ChatGPT subscription/credit invoice.
PRICING_SOURCE = 'https://developers.openai.com/api/docs/models/gpt-5.6-luna'
LUNA_RATES = {'input': '0.20', 'cached_input': '0.02', 'cache_write': '0.25', 'output': '1.20'}
USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def now():
    return datetime.now(timezone.utc).isoformat()


def estimate(usage, model, *, per_request=False):
    if model != 'gpt-5.6-luna':
        raise ValueError('No verified pricing for ' + str(model))
    required = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in required):
        raise ValueError('Missing or invalid input/cache/output token breakdown')
    inputs, cached, output = (usage[k] for k in required)
    if cached > inputs:
        raise ValueError('Cached tokens exceed input tokens')
    if 'total_tokens' in usage and usage['total_tokens'] != inputs + output:
        raise ValueError('Total tokens do not match input plus output')
    writes = usage.get('cache_write_input_tokens')
    if writes is not None and (type(writes) is not int or not 0 <= writes <= inputs - cached):
        raise ValueError('Invalid cache-write tokens')
    r = {k: Decimal(v) for k, v in LUNA_RATES.items()}
    def price(write_count, long_context):
        prompt = (inputs - cached - write_count) * r['input'] + cached * r['cached_input'] + write_count * r['cache_write']
        return (prompt * (2 if long_context else 1) + output * r['output'] * (Decimal('1.5') if long_context else 1)) / 1_000_000
    # Missing cache-write detail uses ordinary input pricing, explicitly an
    # estimate. Context premiums apply to individual responses, never a run sum.
    return float(price(writes or 0, per_request and inputs > 272_000))


class UsageReceipt:
    def __init__(self, request_file, model, effort, service_tier):
        request_file = Path(request_file).resolve(strict=True)
        directory = request_file.parent / 'model-usage'
        directory.mkdir(exist_ok=True)
        self.path = directory / (str(uuid.uuid4()) + '.json')
        self.data = {'version': 2, 'invocation_id': self.path.stem, 'request_file': str(request_file),
            'started_at': now(), 'finished_at': None, 'model': model, 'reasoning_effort': effort,
            'requested_service_tier': service_tier, 'thread_id': None, 'status': 'running',
            'usage': None, 'estimated_base_usd': None, 'actual_model_billed_usd': None,
            'responses': [], 'compaction_response_ids': [], 'usage_reconciled': False,
            'pricing_source': PRICING_SOURCE, 'pricing_checked_on': '2026-09-13',
            'pricing_basis': 'standard_api_equivalent_not_actual_billing',
            'limitations': ['Standard rates exclude Fast/priority premiums and hosted-tool charges.',
                            'Actual subscription charges require billing data; token prices are API-equivalent.']}
        with self.path.open('x', encoding='utf-8') as stream:
            json.dump(self.data, stream)

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)

    def observe(self, event):
        if event.get('type') == 'thread.started':
            if self.data['thread_id'] is not None and self.data['thread_id'] != event.get('thread_id'):
                raise ValueError('Worker thread identity changed')
            self.data['thread_id'] = event.get('thread_id')
            self.save()
        elif event.get('type') == 'turn.completed':
            if self.data['usage'] is not None:
                raise ValueError('Unexpected duplicate completed turn; preserve receipt for reconciliation')
            usage = event.get('usage') or {}
            self.data['usage'] = {k: usage[k] for k in USAGE_FIELDS if k in usage}
            self.save()
        elif event.get('type') in {'error', 'turn.failed'}:
            message = str(event.get('message', event.get('error', ''))).casefold()
            # Keep the failure category, not a second copy of private tool data.
            self.data['failure_kind'] = ('model_usage_limit' if any(term in message for term in
                ('usage limit', 'quota', 'rate limit', 'limit reached')) else
                'model_connection_error' if any(term in message for term in ('connection', 'stream disconnected', 'network'))
                else 'worker_error')
            self.save()

    def observe_response(self, payload, timestamp, model):
        if payload.get('thread_id') != self.data['thread_id']:
            raise ValueError('Usage record belongs to another worker')
        response_id = payload.get('response_id')
        turn_id = payload.get('turn_id')
        if not isinstance(response_id, str) or not response_id or not turn_id:
            raise ValueError('Usage record lacks response/turn identity')
        usage = {k: payload.get('usage', {})[k] for k in USAGE_FIELDS if k in payload.get('usage', {})}
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_FIELDS):
            raise ValueError('Per-response usage lacks the complete numeric breakdown')
        if usage['reasoning_output_tokens'] > usage['output_tokens']:
            raise ValueError('Reasoning output exceeds total output')
        record = {'response_id': response_id, 'turn_id': turn_id, 'model': model,
                  'usage': usage, 'recorded_at': timestamp}
        for key in ('session_id', 'root_turn_id'):
            if isinstance(payload.get(key), str):
                record[key] = payload[key]
        for key in ('turn_token_usage', 'thread_token_usage'):
            value = payload.get(key)
            if isinstance(value, dict):
                record[key] = {k: value[k] for k in USAGE_FIELDS
                               if type(value.get(k)) is int and value[k] >= 0}
        for previous in self.data['responses']:
            if previous['response_id'] == response_id:
                if any(previous[k] != record[k] for k in ('turn_id', 'model', 'usage')):
                    raise ValueError('Conflicting usage for the same response')
                return
        record['estimated_base_usd'] = estimate(usage, model, per_request=True)
        self.data['responses'].append(record)
        self.data['estimated_base_usd'] = float(sum((Decimal(str(r['estimated_base_usd'])) for r in self.data['responses']), Decimal(0)))
        self.save()

    def observe_compaction(self, response_id):
        # This is the runtime's explicit linkage, never a guessed token difference.
        if not isinstance(response_id, str) or not response_id:
            raise ValueError('Compaction event lacks response identity')
        if response_id not in self.data['compaction_response_ids']:
            self.data['compaction_response_ids'].append(response_id)
            self.save()

    def capture_error(self, exc):
        message = str(exc)[:300]
        errors = self.data.setdefault('capture_errors', [])
        if message not in errors:
            errors.append(message)
            self.save()

    def finish(self, exit_code):
        responses = self.data['responses']
        totals = {k: sum(r['usage'][k] for r in responses) for k in USAGE_FIELDS}
        self.data['response_usage_totals'] = totals if responses else None
        final = self.data['usage']
        compactions = set(self.data['compaction_response_ids'])
        ordinary = {k: sum(r['usage'][k] for r in responses if r['response_id'] not in compactions)
                    for k in USAGE_FIELDS}
        complete = bool(responses and final and all(k in final for k in
                       ('input_tokens', 'cached_input_tokens', 'output_tokens')))
        linked = compactions <= {r['response_id'] for r in responses}
        basis = ('all_responses' if complete and all(totals[k] == v for k, v in final.items()) else
                 'cli_excludes_compaction' if complete and compactions and linked
                 and all(ordinary[k] == v for k, v in final.items()) else None)
        self.data['usage_reconciled'] = bool(basis and linked)
        self.data['reconciliation_basis'] = basis
        self.data['compaction_usage_totals'] = {k: totals[k] - ordinary[k] for k in USAGE_FIELDS}

        if responses:
            self.data['estimated_base_usd'] = float(sum(
                (Decimal(str(r['estimated_base_usd'])) for r in responses), Decimal(0)))
        if not self.data['usage_reconciled']:
            self.capture_error('Per-response journal is missing or does not reconcile with the final usage totals')
        self.data.update(exit_code=exit_code, finished_at=now(),
                         status='complete' if exit_code == 0 and self.data['estimated_base_usd'] is not None
                         and self.data['usage_reconciled'] and not self.data.get('capture_errors') else 'incomplete')
        self.save()


class UsageJournal:
    """Read only this worker's temporary journal; retain no prompts or tool data."""

    def __init__(self, profile, receipt):
        self.profile, self.receipt = Path(profile), receipt
        self.path, self.offset, self.discarding = None, 0, False
        self.model = receipt.data['model']

    def poll(self):
        thread_id = self.receipt.data['thread_id']
        if not thread_id:
            return
        uuid.UUID(thread_id)  # Never interpolate arbitrary event data into a path glob.
        if self.path is None:
            paths = list((self.profile / 'sessions').glob('*/*/*/*' + thread_id + '.jsonl'))
            if not paths:
                return
            if len(paths) != 1:
                raise ValueError('Multiple journals found for the worker')
            self.path = paths[0].resolve()
            self.path.relative_to(self.profile.resolve())
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while True:
                record_start = stream.tell()
                line = stream.readline(65537)
                if not line:
                    break
                if not line.endswith(b'\n') and len(line) < 65537:
                    break  # The journal writer has not finished this record yet.
                self.offset = stream.tell()
                # Compaction records contain large private replacement histories.
                # Parse only a bounded record, retain just its response identity.
                if not self.discarding and b'"type":"compacted"' in line[:200].replace(b' ', b''):
                    if not line.endswith(b'\n'):
                        line += stream.readline(16 * 1024 * 1024)
                    if not line.endswith(b'\n'):
                        if len(line) >= 16 * 1024 * 1024:
                            raise ValueError('Compaction metadata exceeds capture limit')
                        self.offset = record_start
                        break
                    self.offset = stream.tell()
                    record = json.loads(line)
                    payload = record.get('payload') if isinstance(record, dict) else None
                    if not isinstance(payload, dict):
                        raise ValueError('Invalid compaction metadata')
                    self.receipt.observe_compaction(payload.get('compaction_response_id'))
                    continue
                if self.discarding or len(line) > 65536:
                    if not self.discarding and b'"token_usage_record"' in line[:1024]:
                        raise ValueError('Usage record exceeded the metadata size limit')
                    self.discarding = not line.endswith(b'\n')
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError('Invalid worker journal record')
                kind, payload = record.get('type'), record.get('payload', {})
                if not isinstance(payload, dict):
                    raise ValueError('Invalid worker journal payload')
                if kind == 'turn_context':
                    self.model = payload.get('model') or self.model
                elif kind == 'token_usage_record':
                    self.receipt.observe_response(payload, record.get('timestamp'), self.model)
                elif kind == 'event_msg' and payload.get('type') in ('model_reroute', 'model_rerouted'):
                    raise ValueError('Model rerouted; billing needs the actual response model')


def execute_with_usage(command, cwd, env, receipt, *, profile=None, deadline=None, cost_stop=None):
    """Capture usage; optionally stop even a silent worker at an absolute deadline.

    deadline is a callable so the normalized, saved user limit takes precedence
    as soon as setup completes. Terminate this worker's process group only.
    """
    code = None
    stopped = threading.Event()
    watchdog = None
    termination = {}
    journal = UsageJournal(profile, receipt) if profile is not None else None
    capture_lock = threading.RLock()
    def capture_journal():
        with capture_lock:
            if journal:
                try:
                    journal.poll()
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    receipt.capture_error(exc)
    def stop_group(child):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                # Still stop the owned leader if a platform denies group signals.
                # Record the group-cleanup failure; never report clean completion.
                termination['cleanup_error'] = 'Worker process-group cleanup was denied'
                if child.poll() is None:
                    child.send_signal(sig)
            if sig == signal.SIGTERM:
                # A descendant can still hold stdout open after its parent exits.
                time.sleep(0.5)
                # Reap the leader before probing the group again. macOS can
                # return EPERM for a group containing only its zombie leader.
                child.poll()

    def watch(child):
        while not stopped.wait(1):
            try:
                capture_journal()
                reason = cost_stop() if cost_stop else None
                limit = deadline() if deadline else None
                if not reason and (limit is None or time.time() < limit):
                    continue
                termination['failure_kind'] = reason or 'deadline_reached'
            except Exception as exc:
                termination.update(failure_kind='invalid_saved_state', deadline_error=str(exc)[:500])
            stop_group(child)
            return

    try:
        with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, text=True, encoding='utf-8',
                              start_new_session=True) as child:
            if deadline is not None or cost_stop is not None:
                watchdog = threading.Thread(target=watch, args=(child,), daemon=True)
                watchdog.start()
            try:
                for line in child.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    # Only small metadata events can contain retained usage.
                    if len(line) <= 65536:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            with capture_lock:
                                try:
                                    receipt.observe(event)
                                except (ValueError, OSError, TypeError) as exc:
                                    receipt.capture_error(exc)
                    capture_journal()
                code = child.wait()
            except KeyboardInterrupt:
                termination['failure_kind'] = 'cancelled'
                raise
            finally:
                stopped.set()
                if watchdog:
                    watchdog.join(timeout=2)
                if child.poll() is None or termination:
                    stop_group(child)
                code = child.wait()
    finally:
        receipt.data.update(termination)
        capture_journal()  # The worker has flushed its journal before profile cleanup.
        receipt.finish(code)
    return code or (0 if receipt.data['status'] == 'complete' else 2)


def report(results, receipt_paths, run_directory=None, *, provider_accounting=None):
    """One known subtotal. Unknown charges stay pending, never projected."""
    provider_usd, pending = Decimal(0), 0
    provider_missing = []
    if provider_accounting is not None:
        for row in provider_accounting['providers'].values():
            provider_usd += Decimal(str(row['billed_usd']))
            pending += row['unresolved_calls']
    else:
        costs = results.get('cost_summary', {})
        provider_usd = Decimal(str(costs.get('deepline', {}).get('confirmed_usd') or 0))
        sd = costs.get('scrapingdog', {})
        if sd.get('confirmed_credits', sd.get('maximum_credits', 0)):
            provider_missing.append('ScrapingDog USD cost needs the saved plan conversion')
        dl = costs.get('deepline', {})
        if 'maximum_usd' in dl and dl['maximum_usd'] != dl.get('confirmed_usd'):
            provider_missing.append('Historical provider billing is incomplete')
        pending = sum(r.get('paid_calls', 0) > 0 and r.get('cost_credits') is None and r.get('cost_usd') is None
                      for r in results.get('routes', []))
    llm, seen, workers, missing = Decimal(0), {}, [], provider_missing
    for path in receipt_paths:
        receipt = json.loads(Path(path).read_text())
        if run_directory is not None and Path(receipt.get('request_file', '')).parent.resolve() != Path(run_directory).resolve():
            raise ValueError('Model receipt belongs to a different run')
        identity = receipt.get('invocation_id')
        if not identity or any(r['invocation_id'] == identity for r in workers):
            raise ValueError('Missing or duplicate model invocation identity')
        workers.append(receipt)
        if not receipt.get('usage_reconciled') or receipt.get('capture_errors'):
            missing.append('Incomplete model usage: ' + identity)
        for response in receipt.get('responses', []):
            rid = response['response_id']
            cost = response.get('estimated_base_usd')
            old = response.get('standard_api_equivalent_usd', {})
            if cost is None and old.get('minimum') == old.get('maximum'):
                cost = old.get('minimum')
            if cost is None:
                missing.append('Unpriced model response: ' + rid)
                continue
            proof = (response.get('model'), response.get('usage'), cost)
            if rid in seen:
                if seen[rid] != proof:
                    raise ValueError('Conflicting model response cost')
                continue
            seen[rid] = proof
            llm += Decimal(str(cost))
    if not workers:
        missing.append('Sourcing model usage was not captured')
    if pending:
        missing.append('Provider billing is pending')
    total = provider_usd + llm
    count = len(results.get('accepted', []))
    return {'status': 'incomplete' if missing else 'calculated', 'scope': 'tyche_run_only',
            'basis': 'reported_provider_charges_plus_estimated_base_llm',
            'provider_usd': float(provider_usd), 'estimated_llm_usd': float(llm),
            'total_usd': float(total), 'pending_provider_calls': pending,
            'cost_per_accepted_lead_usd': float(total / count) if count and not missing else None,
            'worker_invocations': workers, 'accepted_leads': count, 'missing': missing,
            'limitations': ['Pending charges are excluded from the known total, not assumed free.',
                'Completed catalog-free calls are supported by saved unconditional zero-price contracts, not billing receipts.',
                'Model cost uses base API rates, excluding Fast premiums, hosted tools and subscription allocation.',
                'Outer chat, monitoring and development costs are outside this run.']}


def save_report(run_directory, results_path=None):
    """Refresh the saved report from all attempts; no caller-supplied subset."""
    directory = Path(run_directory).resolve()
    results_path = Path(results_path) if results_path is not None else directory / 'results.json'
    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    accounting = None
    ledger_path = results_path.with_name(results_path.name + '.budget.json')
    if ledger_path.exists():
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/skills/lead-sourcing/scripts'))
        import budget_guard
        accounting = budget_guard.accounting_summary(budget_guard.load_ledger(results_path))
    output = report(results, sorted((directory / 'model-usage').glob('*.json')), directory,
                    provider_accounting=accounting)
    if accounting is not None:
        output['provider_accounting'] = accounting
    path = directory / 'run-costs.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    commentary = directory / 'research-commentary.md'
    if commentary.exists():
        write_research_report(directory, results, output, commentary.read_text(encoding='utf-8'))
    return path


def write_research_report(directory, results, costs, commentary):
    """Render saved facts and accounting. Research prose remains agent-authored."""
    def cell(value):
        return str(value if value is not None else 'unknown').replace('|', '\\|').replace('\n', ' ')

    def source(value):
        if not isinstance(value, dict) or not value:
            return 'unknown'
        ref = value.get('source', value)
        label = '/'.join(str(ref[k]) for k in ('provider', 'tool') if ref.get(k)) or 'unknown'
        rid = ref.get('route_id')
        # IDs are generated/validated by the run helpers; never accept a path.
        if rid and all(c.isalnum() or c in '._-' for c in rid):
            label += f' ([{rid}](receipts/{rid}.json))'
        url = value.get('evidence_url', value.get('url'))
        return label + (f' — {url}' if url else '')

    validation_path = directory / 'validation.json'
    validation = json.loads(validation_path.read_text()) if validation_path.exists() else {}
    clock = results.get('stop_check', {})
    def elapsed(end):
        try:
            delta = datetime.fromisoformat(end.replace('Z', '+00:00')) - datetime.fromisoformat(clock['started_at'].replace('Z', '+00:00'))
            seconds = round(delta.total_seconds())
            return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 0 else 'unavailable'
        except (AttributeError, KeyError, TypeError, ValueError):
            return 'unavailable'

    accepted = results.get('accepted', [])
    request = results.get('request', {})
    lines = ['# TYCHE run report', '',
        f"Accepted {len(accepted)} of {request.get('target_count', 'unknown')} requested companies. Stop: {results.get('stop_reason', 'still running')}.",
        f"Started: {clock.get('started_at', 'unavailable')}. Leads ready: {clock.get('leads_ready_at', 'unavailable')}. "
        f"Workbook checked: {validation.get('completed_at', 'unavailable')}.",
        f"Time to leads: {elapsed(clock.get('leads_ready_at'))}. Time to checked workbook: {elapsed(validation.get('completed_at'))}.",
        '', '## Research commentary', '', commentary.strip(), '', '## Run-only costs', '']
    coverage = results.get('summary', {}).get('contact_coverage')
    if coverage:
        lines.insert(3, f"Contacts: {coverage['contacts']} across accepted companies. Minimum per company: "
                     f"{coverage['minimum_per_company']}; target: {coverage['target_per_company']}. "
                     f"Companies at target: {coverage['companies_at_target']}/{len(accepted)}. "
                     f"Additional contacts needed for those companies: {coverage['target_shortfall']}.")
    lines += [f"- Reported provider charges: ${costs['provider_usd']:.4f}.",
              f"- Estimated base LLM cost: ${costs['estimated_llm_usd']:.4f}.",
              f"- Known total: ${costs['total_usd']:.4f}.",
              f"- Provider calls awaiting billing: {costs['pending_provider_calls']}."]
    lines.extend('- ' + note for note in costs.get('missing', []) + costs.get('limitations', []))
    lines += ['', 'Full numeric receipts: [run-costs.json](run-costs.json).', '', '## Accepted-lead sources', '',
              '| Company / domain | Discovery | Fit | Intent | Buyer role | Email lookup | Validation |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    counts = {kind: {} for kind in ('discovery', 'email', 'validation')}
    for row in accepted:
        company, contact = row.get('company', {}), row.get('primary_contact', {})
        discovery = source(company.get('discovery_source', row.get('discovery_source')))
        email = source(contact.get('email_source')) if 'email' in request.get('contact_fields', ['email']) else 'not requested'
        verifier = source(contact.get('email_validation')) if email != 'not requested' else 'not requested'
        values = [f"{company.get('canonical_name', '')} / {company.get('domain', '')}", discovery,
                  source(row.get('account_fit')), source(row.get('signal_evidence')), source(contact), email, verifier]
        lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
        for kind, evidence in [('discovery', discovery), ('email', email), ('validation', verifier)]:
            label = evidence.split(' (')[0]
            counts[kind][label] = counts[kind].get(label, 0) + 1
    lines += ['', 'Source counts (unknown attribution is retained): ' + json.dumps(counts) + '.',
              '', '## Reviewed companies', '', '| State | Company | Decision / remaining gap |', '| --- | --- | --- |']
    for state in ('accepted', 'rejected', 'unresolved'):
        for row in results.get(state, []):
            company = row.get('company', row.get('candidate', {}))
            lines.append('| ' + ' | '.join(cell(v) for v in (state, company.get('domain'), row.get('reason_text', 'Qualified; see saved evidence and contact selection'))) + ' |')
    lines += ['', '## Research routes', '', '| Receipt | Provider / tool | Scope / phase | Status | Rows | Reported credits | Cost basis |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for route in results.get('routes', []):
        values = [route.get('route_id'), source(route), f"{route.get('scope')} / {route.get('phase')}", route.get('provider_status'),
                  route.get('rows_returned'), route.get("cost_credits"), route.get('cost_basis')]
        lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
    lines += ['', '## Saved request and audit', '', 'The request, qualification evidence, contact selections and source frontier are in [results.json](results.json).', '',
              '```json', json.dumps({k: results.get(k) for k in ('request', 'summary', 'cost_summary', 'stop_audit')}, indent=2), '```', '']
    path = directory / 'report.md'
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    args = parser.parse_args()
    try:
        args.results.resolve(strict=True)
        print(save_report(args.results.parent, args.results).read_text(), end='')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + '\n')
