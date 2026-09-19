#!/usr/bin/env python3
"""Launch a fresh, project-only Codex test without changing global settings."""

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

try:
    from .run_costs import UsageReceipt, execute_with_usage, save_report
except ImportError:  # Direct CLI invocation.
    from run_costs import UsageReceipt, execute_with_usage, save_report


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / '.agents' / 'skills'
sys.path.insert(0, str(SKILL_ROOT / 'lead-sourcing' / 'scripts'))
CODEX_VERSION = '0.154.0'
MODEL = 'gpt-5.6-luna'
REASONING_EFFORT = 'high'
SERVICE_TIER = 'fast'
FINALIZATION_SECONDS = 600
STARTUP_SECONDS = 600
MAX_UNCHANGED_EXITS = 5
DEFAULT_WORKERS = 2


def saved_run(request_file):
    path = Path(request_file).resolve().parent / 'results.json'
    return json.loads(path.read_text()) if path.exists() else None


def original_start(request_file, fallback):
    """Even a restart before tyche_start must not receive a fresh clock."""
    document = saved_run(request_file)
    if document is not None:
        return document['stop_check']['started_at']
    ledger_path = Path(request_file).resolve().parent / 'results.json.budget.json'
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text())
        if ledger.get('initial_started_at'):
            return ledger['initial_started_at']
    starts = [fallback]
    for path in (Path(request_file).resolve().parent / 'model-usage').glob('*.json'):
        receipt = json.loads(path.read_text())
        if Path(receipt['request_file']).resolve() == Path(request_file).resolve():
            starts.append(receipt.get('run_started_at', receipt['started_at']))
    return min(starts, key=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')))


def research_deadline(request_file, started_at):
    from validate_run import run_deadline
    document = saved_run(request_file)
    if document is None:
        return None
    limit = run_deadline(document)
    return limit.timestamp() if limit is not None else None


def authorize_resume(request_file, until, reason):
    """Operator-only amendment: keep request, original start and ledger intact."""
    import budget_guard
    from validate_run import run_deadline
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('--resume-reason must record the user authorization')
    revised = datetime.fromisoformat(until.replace('Z', '+00:00'))
    if revised.utcoffset() is None or revised <= datetime.now(timezone.utc):
        raise ValueError('--resume-until must be a future timezone-aware timestamp')
    run_file = Path(request_file).resolve().parent / 'results.json'
    state = budget_guard.load_ledger(run_file)
    if state is None:
        raise ValueError('Resume requires an existing run and ledger')
    with budget_guard.transaction(run_file) as document:
        deadline = run_deadline(document)
        if deadline is None:
            raise ValueError('This run has no research deadline to extend')
        extensions = document['stop_check'].get('research_extensions', [])
        if extensions and revised == deadline and extensions[-1]['authorization'] == reason:
            return  # Retrying the same launch cannot grant additional time.
        if revised <= deadline:
            raise ValueError('--resume-until must extend the saved deadline')
        errors = budget_guard.audit_ledger(run_file, document, state=state, allow_pending=True)
        if errors:
            raise ValueError('Reconcile saved accounting before resuming: ' + '; '.join(errors))
        document['stop_check'].setdefault('research_extensions', []).append({
            'previous_deadline': deadline.isoformat(), 'deadline': revised.isoformat(),
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'authorization': reason})
        run_deadline(document)


def cost_stop(request_file, active_model_receipt=None, *, admission=False):
    """The same observed-cost threshold used by provider dispatch.

    ``admission`` returns the raw observed spend decision without the
    active-owner suppression used only to drain an admitted response.
    """
    import budget_guard
    run_file = Path(request_file).resolve().parent / 'results.json'
    if not run_file.exists():
        return None  # The first response initializes the authoritative run.
    state = budget_guard.load_ledger(run_file)
    if state is None or state['version'] != 2:
        return None
    # Settlement precedes response/route persistence. Let both writes finish
    # before terminating the worker that owns the dispatch.
    document = saved_run(request_file)
    recorded = {r['route_id'] for r in document.get('routes', [])}
    if not admission and (any(c.get('state') == 'in_flight' for c in state['calls'].values())
                          or set(state['calls']) - recorded):
        return None
    reason = budget_guard.spending_stop(state, accepted_count=len(document['accepted']),
                                       active_model_receipt=active_model_receipt)
    # Provider dispatch is already paused. Let the current model response close
    # its usage normally before billing recovery; killing it would create a
    # second, irrecoverable missing-usage problem solely from a delayed bill.
    return (None if not admission and active_model_receipt and reason == 'billing_pending'
            else reason)


def supervise_worker(command, request_file, env, profile, *, resume=False, host=None):
    """Continue saved research, not paid calls. Completion is a checked artifact."""
    import run_coordination as coordination
    host = host or LocalHost()
    run_file = Path(request_file).resolve().parent / 'results.json'
    try:
        with coordination.locked(run_file, "supervisor", blocking=False):
            shared = coordination.snapshot(run_file)
            if shared and shared['worker_count'] != int(env.get('TYCHE_PARALLEL_WORKERS', '1')):
                raise ValueError('Resume this run with its original parallel worker count')
            recover_stopped_workers(run_file, receipts_directory=host.receipts_directory)
            with (run_file.parent / 'launcher.log').open('a', encoding='utf-8') as output:
                from contextlib import redirect_stdout
                with redirect_stdout(output):
                    return _supervise_worker(command, request_file, env, profile, resume=resume, host=host)
    except BlockingIOError:
        print('This saved run already has an active supervisor; no duplicate started', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
            'reason': 'invalid_or_unavailable_runtime_state', 'detail': str(exc)[:2000],
            'resume': 'Repair the saved state or runtime; preserve the original clock, ledger and receipts.'})
        print('TYCHE runtime blocked: ' + str(exc), file=sys.stderr)
        return 1


def recover_stopped_workers(run_file, *, receipts_directory='model-usage'):
    """Only the exclusive supervisor may close abandoned invocation state."""
    import run_coordination as coordination
    state = coordination.snapshot(run_file)
    if state:
        for worker in state['workers'].values():
            if worker['status'] == 'running' and not (run_file.parent / receipts_directory / (worker['generation'] + '.json')).is_file():
                raise ValueError('Running worker receipt is missing; preserve invocation state')
    # A killed supervisor may leave its child process group alive. Never reuse
    # ownership until that group is gone, even if the saved receipt says stopped.
    for path in (run_file.parent / receipts_directory).glob('*.json'):
        receipt = json.loads(path.read_text())
        pid = receipt.get('process_group_id')
        if pid is not None:
            if type(pid) is not int or pid <= 1:
                raise ValueError('Invalid saved process group; preserve invocation state')
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise BlockingIOError('A saved worker process group is still alive')
        elif state and not receipt.get('finished_at') and any(
                row.get('generation') == path.stem and row.get('status') == 'running'
                for row in state['workers'].values()):
            raise ValueError('Legacy worker has no process identity; verify its exit before recovery')
    if state:
        def close(value):
            for worker in value['workers'].values():
                worker['status'] = 'stopped'
            value['phase'] = 'finalization'
        coordination.update(run_file, close)


def _supervise_worker(command, request_file, env, profile, *, resume=False, host=None):
    from research_tools import ResearchTools
    from run_attempt import recover_completed_attempts
    from validate_run import DELIVERY_STOPS
    host = host or LocalHost()
    request_file = Path(request_file).resolve()
    run_file = request_file.parent / 'results.json'
    import run_coordination as coordination
    shared = coordination.snapshot(run_file)
    if shared is not None and shared['worker_count'] != int(env.get('TYCHE_PARALLEL_WORKERS', '1')):
        raise ValueError('Resume this run with its original parallel worker count')
    env = dict(env, TYCHE_RUN_STARTED_AT=original_start(request_file, env['TYCHE_RUN_STARTED_AT']))
    finishing_until = None
    research_window_closed = False
    failed_exits = 0
    unchanged_exits = 0
    attempt = 0
    continuation = ('Continue the SAME saved run at ' + str(run_file) + '. '
        'Read the local skill. Preserve the saved request, start time, ledger, '
        'pending calls and evidence. The saved request is authoritative. Judge current evidence, including newer receipts; '
        'historical feedback is a concern to verify, not a verdict to repeat after it has been resolved. '
        'Recover saved responses; never replay an uncertain paid call. '
        'An empty queue or exhausted search approach requires a different strategy, not completion. ')
    while True:
        document = saved_run(request_file)
        if attempt and document is None:
            # No authoritative budget exists yet. Preserve the first worker's
            # usage, but never spend on automatic retries of incomplete setup.
            blocked = {'reason': 'run_not_initialized',
                'resume': 'Repair startup before explicitly resuming this saved request. Preserve its clock and usage receipts.'}
            startup_status = run_file.parent / 'operational-status.json'
            if startup_status.exists():
                startup = json.loads(startup_status.read_text())
                if startup.get('status') == 'operationally_blocked':
                    blocked.update(startup)
            write_worker_status(request_file, dict(blocked, status='blocked',
                delivery_allowed=False, run_file=str(run_file)))
            return 1
        if document is not None:
            if status := host.before_recovery(request_file, env):
                write_worker_status(request_file, status)
                return 1
            recovery = recover_completed_attempts(run_file)
            if recovery['errors']:
                write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
                    'reason': 'saved_dispatch_accounting_incomplete', 'run_file': str(run_file),
                    'recovery': recovery,
                    'resume': 'Preserve the original clock, ledger and receipts. Resume after saved responses or billing evidence reconcile the pending calls; never replay paid requests.'})
                return 1
            if status := host.prepare(request_file, env):
                write_worker_status(request_file, status)
                return 1
        progress = ResearchTools(run_file, environment=env)._overview() if document is not None else {}
        limit = research_deadline(request_file, env['TYCHE_RUN_STARTED_AT'])
        stop = progress.get('stop')
        terminal = (stop in DELIVERY_STOPS or research_window_closed or (limit is not None and time.time() >= limit)
                    or (finishing_until is not None and stop != 'continue'))
        blocked = progress.get('operational_block') or (
            stop if stop in {'provider_stop', 'input_or_configuration_stop'} else None)
        recovering_access = (resume and attempt == 0 and not terminal and isinstance(blocked, str)
            and any(blocked.startswith(tool + ': ' + status) for tool in
                    ('harvestapi_get_company', 'harvestapi_get_profile')
                    for status in ('auth_failed', 'quota_exceeded')))
        if blocked and document is not None and not recovering_access:
            # A free refresh may repair access after funding/authentication is restored.
            # The original research clock, paid-call gates and ledger remain binding.
            if host.recover_access(run_file, env):
                continue
        if blocked and not recovering_access:
            status = {'status': 'blocked', 'delivery_allowed': False, 'reason': str(blocked), 'run_file': str(run_file)}
            status['partial_export'] = host.export_partial(run_file, env)
            write_worker_status(request_file, status)
            return 1
        if not terminal:
            # A semantic review may demote a row after reaching the target.
            # Re-evaluate the saved clock/budget; finalization is not a new stop reason.
            finishing_until = None
            if int(env.get('TYCHE_PARALLEL_WORKERS', '1')) > 1:
                try:
                    from .parallel_sourcing import run_research
                except ImportError:
                    from parallel_sourcing import run_research
                research_command = list(command)
                if attempt:
                    research_command[-1] = continuation
                if recovering_access:
                    research_command[-1] = continuation + ('The user reports restored provider access. '
                        'Refresh the affected free description with tyche_inspect(tool=..., refresh=true). '
                        'Preserve failed receipts and reservations; do not repeat that request.')
                run_research(research_command, request_file, env, profile, int(env['TYCHE_PARALLEL_WORKERS']), host=host)
                attempt += 1
                continue
        elif finishing_until is None:
            # Review may use the original remaining time. The short grace is
            # for runs at their deadline, not an earlier cap on a valid repair.
            finishing_until = host.finalization_deadline(max(time.time(), limit or 0) + FINALIZATION_SECONDS)
        if finishing_until is not None and time.time() >= finishing_until:
            write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
                'reason': 'finalization_timeout', 'run_file': str(run_file),
                'resume': 'Review/export saved evidence only; do not reopen sourcing or reset accounting.'})
            return 1
        # Research and final review use separate contexts, sharing the same run.
        worker_env = dict(env, TYCHE_FINALIZATION_ONLY='1' if terminal else '0')
        worker_command = list(command)
        if recovering_access:
            worker_command[-1] = continuation + ('The user reports restored provider access. '
                'First refresh the affected free tool description with tyche_inspect(tool=..., refresh=true). '
                'Preserve the failed paid receipt and reservation; do not repeat that request. '
                'If access remains blocked, return the blocker. Otherwise continue useful research.')
        if attempt or terminal:
            worker_command[-1] = (('Current invocation feedback (check against current evidence):\n' + command[-1] + '\n\n') if not attempt else '') + continuation + ('Research has stopped. Request the final evidence packet with '
                'tyche_finish before individual field inspections, then follow its review instructions. '
                'If corrections leave the target incomplete, save them and return; the supervisor will '
                're-evaluate remaining time and budget before allowing more research.' if terminal else
                'Use tyche_inspect first. Continue useful sourcing while budget and time remain, then review and deliver.')
        startup_until = time.time() + STARTUP_SECONDS
        def worker_deadline():
            return (research_deadline(request_file, env['TYCHE_RUN_STARTED_AT'])
                    if run_file.exists() else startup_until)
        before = run_file.read_bytes() if run_file.exists() else None
        status, execution = host.run_once(
            worker_command, request_file, worker_env, profile,
            deadline=(lambda: finishing_until) if terminal else worker_deadline,
            terminal=terminal, attempt=attempt)
        if status['delivery_allowed']:
            return 0 if execution['status'] == 'complete' else 2
        failure = execution.get('failure_kind')
        if failure == 'deadline_reached':
            if terminal:
                status.update(status='blocked', reason='finalization_timeout')
                write_worker_status(request_file, status)
                return 1
            research_window_closed = True
        if execution.get('cleanup_error') or failure in {'cancelled', 'model_usage_limit', 'invalid_saved_state', 'startup_timeout', 'host_limit'}:
            status.update(status='blocked', reason=execution.get('cleanup_error') or failure)
            write_worker_status(request_file, status)
            return 1
        failed_exits = failed_exits + 1 if execution.get('exit_code') and failure != 'deadline_reached' else 0
        if failed_exits >= 2:
            status.update(status='blocked', reason='repeated_worker_failure')
            write_worker_status(request_file, status)
            return 1
        unchanged_exits = unchanged_exits + 1 if (not execution.get('exit_code') and
            before == (run_file.read_bytes() if run_file.exists() else None)) else 0
        if unchanged_exits >= MAX_UNCHANGED_EXITS:
            status.update(status='blocked', reason='repeated_worker_no_progress')
            write_worker_status(request_file, status)
            return 1
        attempt += 1
        print(json.dumps({'continuing_saved_run': str(run_file), 'reason': failure or 'premature_worker_exit'}), flush=True)



class LocalHost:
    """Local authentication, usage receipts and workbook delivery for the shared loop."""

    receipts_directory = 'model-usage'

    @staticmethod
    def research_receipt(request_file):
        return UsageReceipt(request_file, MODEL, REASONING_EFFORT, SERVICE_TIER)

    @staticmethod
    def execute_research(command, request_file, env, receipt, **options):
        # Import at invocation time, as the original pool did.
        try:
            from .run_costs import execute_with_usage
        except ImportError:
            from run_costs import execute_with_usage
        env['TYCHE_ACTIVE_MODEL_RECEIPT'] = receipt.path.stem
        return execute_with_usage(command, ROOT, env, receipt, **options)

    @staticmethod
    def reconcile_research(run_file):
        from billing_reconciliation import reconcile
        return reconcile(run_file)

    @staticmethod
    def research_report(run_dir):
        return save_report(run_dir)

    @staticmethod
    def before_recovery(request_file, env):
        return None

    @staticmethod
    def recover_access(run_file, env):
        from research_tools import ResearchTools
        return ResearchTools(run_file, environment=env).recover_access()

    @staticmethod
    def finalization_deadline(proposed):
        return proposed

    @staticmethod
    def export_partial(run_file, env):
        from research_tools import ResearchTools
        return ResearchTools(run_file, environment=env).export_partial()

    def prepare(self, request_file, env):
        run_file = request_file.parent / 'results.json'
        import budget_guard
        ledger = budget_guard.load_ledger(run_file)
        if ledger and ledger['version'] == 2:
            from billing_reconciliation import reconcile
            billing_until = time.monotonic() + 120
            reconcile(run_file)  # Read-only settlement also helps stopped/incomplete runs.
            if cost_stop(request_file) == 'billing_pending':
                from billing_reconciliation import wait_for_billing
                write_worker_status(request_file, {'status': 'waiting', 'delivery_allowed': False,
                    'reason': 'billing_pending', 'run_file': str(run_file)})
                wait_for_billing(run_file, deadline=research_deadline(request_file, env['TYCHE_RUN_STARTED_AT']),
                                 max_wait_seconds=max(0, billing_until - time.monotonic()))
        if reason := cost_stop(request_file):
            from confirmed_leads import update
            audit = None
            if reason == 'budget_exhausted':
                from run_attempt import save_stop_checkpoint
                audit = save_stop_checkpoint(run_file)
            update(run_file)  # Preserve reviewed partial output without another model turn.
            return {'status': 'stopped', 'delivery_allowed': False,
                'reason': reason, 'run_file': str(run_file), 'stop_validation': audit,
                'partial_output': str(run_file.parent / 'leads.json'),
                'run_cost_report': str(save_report(request_file.parent))}
        return None

    def run_once(self, worker_command, request_file, worker_env, profile, *, deadline,
                 terminal, attempt):
        run_file = request_file.parent / 'results.json'
        receipt = UsageReceipt(request_file, MODEL, REASONING_EFFORT, SERVICE_TIER)
        receipt.data['run_started_at'] = worker_env['TYCHE_RUN_STARTED_AT']
        receipt.save()
        worker_env['TYCHE_ACTIVE_MODEL_RECEIPT'] = receipt.path.stem
        print(json.dumps({'model_usage_receipt': str(receipt.path), 'attempt': attempt + 1,
                          'phase': 'finalization' if terminal else 'research'}), flush=True)
        try:
            execute_with_usage(worker_command, ROOT, worker_env, receipt, profile=profile,
                cost_stop=lambda: cost_stop(request_file, receipt.path.stem),
                deadline=deadline)
        finally:
            if receipt.data.get('failure_kind') == 'deadline_reached' and not run_file.exists():
                receipt.data['failure_kind'] = 'startup_timeout'
                receipt.save()
            status_path = close_worker(request_file, receipt, worker_env)
            print(json.dumps({'worker_status': str(status_path),
                              'run_cost_report': str(save_report(request_file.parent))}), flush=True)
        status = json.loads(status_path.read_text())
        return status, receipt.data

def write_worker_status(request_file, status):
    output = Path(request_file).resolve().parent / 'worker-status.json'
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(status, indent=2) + '\n', encoding='utf-8')
    temporary.replace(output)
    return output


def close_worker(request_file, receipt, environment=None):
    """One local export recovery after explicit review; never relaunch research."""
    directory = Path(request_file).resolve().parent
    path = directory / 'results.json'
    from research_tools import ResearchTools
    from run_attempt import delivery_preflight, review_fingerprint
    from validate_run import contact_coverage, sourcing_target_met
    def current_invocation(value):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            started = datetime.fromisoformat(receipt.data['started_at'].replace('Z', '+00:00'))
            return stamp >= started
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
    def delivered():
        validation = directory / 'validation.json'
        workbook = directory / 'leads.xlsx'
        if not (path.exists() and validation.exists() and workbook.exists()):
            return False
        saved = json.loads(validation.read_text())
        current = json.loads(path.read_text())
        return (saved.get('delivery_allowed') is True
                and current_invocation(saved.get('completed_at'))
                and delivery_preflight(path, current)[1]['delivery_allowed']
                and current.get('final_review', {}).get('review_ref') == review_fingerprint(current)
                and saved.get('results_sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
                and saved.get('workbook_sha256') == hashlib.sha256(workbook.read_bytes()).hexdigest())
    status = {'status': 'interrupted', 'delivery_allowed': False, 'artifact_verified': False,
              'target_met': False, 'run_file': str(path),
              'worker_exit_code': receipt.data.get('exit_code'), 'reason': receipt.data.get('failure_kind', 'worker_ended_before_delivery'),
              'resume': 'Resume this saved run and ledger. Do not restart accounting or repeat uncertain paid requests.'}
    try:
        document = json.loads(path.read_text()) if path.exists() else {}
        status['accepted_count'] = len(document.get('accepted', []))
        status['target_count'] = document.get('request', {}).get('target_count')
        reviewed = (document.get('final_review', {}).get('review_ref') == review_fingerprint(document)
                    and current_invocation(document.get('final_review', {}).get('reviewed_at')))
        if not delivered() and reviewed and receipt.data.get('failure_kind') != 'cancelled':
            # The agent already approved this exact research. Retry only the
            # deterministic finish, once, with existing budget/evidence gates.
            status['finish_recovery'] = ResearchTools(path, environment=environment).finish()
        if delivered():
            # Export success and reaching the requested count are different outcomes.
            document = json.loads(path.read_text())
            status['accepted_count'] = len(document['accepted'])
            status['target_count'] = document['request']['target_count']
            target_met = sourcing_target_met(document)
            status.update(status='complete' if target_met else 'partial', delivery_allowed=True,
                          artifact_verified=True, target_met=target_met,
                          contact_coverage=contact_coverage(document),
                          shortfall=max(0, status['target_count'] - status['accepted_count']),
                          stop_reason=document.get('stop_reason'), reason='verified_saved_workbook')
        elif not reviewed and not receipt.data.get('failure_kind'):
            status.update(status='review_required' if status['target_count'] and sourcing_target_met(document) else 'incomplete',
                          reason='research_or_review_still_required')
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        status['finish_error'] = str(exc)[:2000]
    return write_worker_status(request_file, status)
ISOLATION_INSTRUCTIONS = (
    'You are already inside the isolated TYCHE runtime (TYCHE_ISOLATED_RUN=1). '
    'You are the sourcing worker, not the outer monitor. '
    'Launcher state, logs, model-usage receipts and monitor notes in your assigned '
    'run directory describe this invocation; their presence is not evidence of '
    'a separate worker. Do not wait for those artifacts to produce sourcing results. '
    'Never move, delete or recreate the assigned run directory or its managed state '
    'to fix setup or tool errors. If the saved request is wrong, report the blocker '
    'with the original run, ledger, receipts and clock intact. '
    'Execute sourcing requests directly with the local lead-sourcing skill. '
    'Never launch scripts/codex_tyche.py from this runtime. '
    'Use project-local instructions and skills. '
    'Do not read or use user-global AGENTS.md, skills, or memories. '
    'Normal system instructions, managed permissions, and execution rules still apply. '
    'Use the project lead-sourcing skill for sourcing requests. '
    'When TYCHE native tools are available, use them for all run setup, provider '
    'lookups, saving reviews, inspecting state and finalization. The tools are '
    'bound to this run. Read source code or use diagnostic CLIs only for a '
    'specific tool failure; ordinary research needs no shell bookkeeping.'
)
SMOKE_PROMPT = (
    'This is a read-only configuration smoke test, not a sourcing job. '
    'List the available skill names from your supplied skill catalog. '
    'Read .agents/skills/lead-sourcing/SKILL.md and state the required deliverable filenames. '
    'Do not read global instruction or skill files, call providers, search the web, '
    'delegate work, or modify files. Call tyche_inspect with no arguments twice '
    'in sequence to verify the native tool connection can be reused; both calls '
    'are read-only and make no provider calls.'
)


def codex_binary(env):
    """Use the same released client as Arena, without changing the global CLI."""
    installed = ROOT / '.runtime/node_modules/.bin/codex'
    binary = env.get('TYCHE_CODEX_BINARY') or (str(installed) if installed.is_file() else 'codex')
    version = subprocess.run([binary, '--version'], env=env, capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
    if version != 'codex-cli ' + CODEX_VERSION:
        raise RuntimeError('TYCHE requires Codex ' + CODEX_VERSION + ' to match Arena; found ' + version +
            '. Install with npm install --prefix .runtime --no-audit --no-fund --save-exact @openai/codex@' +
            CODEX_VERSION + ', or set TYCHE_CODEX_BINARY to that version.')
    return binary


def inside(path, directory):
    try:
        Path(path).resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def workspace_environment(env):
    """Use configured paths or the installed desktop bundle, without downloads."""
    env = dict(env)
    bundle = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies'
    defaults = {
        'TYCHE_WORKSPACE_NODE': bundle / 'node/bin/node',
        'TYCHE_WORKSPACE_NODE_MODULES': bundle / 'node/node_modules',
        'TYCHE_WORKSPACE_PYTHON': bundle / 'python/bin/python3',
    }
    for key, default in defaults.items():
        if not env.get(key) and default.exists():
            env[key] = str(default)
    if env.get('TYCHE_WORKSPACE_NODE'):
        env['PATH'] = str(Path(env['TYCHE_WORKSPACE_NODE']).parent) + os.pathsep + env.get('PATH', '')
    return env


def tool_configuration(run_file, *, readonly=False):
    """Bind the MCP process to this run and the existing command sandbox.

    The stdio relay obtains sandbox metadata from Codex on the first tool call.
    Research executes in its child with that exact filesystem/network policy.
    """
    args = [str(SKILL_ROOT / 'lead-sourcing/scripts/tyche_tools.py'),
            '--run-file', str(Path(run_file).resolve())]
    if readonly:
        args.append('--read-only')
    forwarded = ['CODEX_HOME', 'DEEPLINE_API_KEY', 'DEEPLINE_BIN', 'SCRAPINGDOG_API_KEY',
                 'DEEPLINE_NO_AUTO_UPDATE', 'DEEPLINE_SKIP_SKILLS_SYNC', 'TYCHE_WORKSPACE_NODE',
                 'TYCHE_WORKSPACE_NODE_MODULES', 'TYCHE_WORKSPACE_PYTHON', 'PYTHONDONTWRITEBYTECODE',
                 'TYCHE_RUN_STARTED_AT', 'TYCHE_REQUEST_FILE', 'TYCHE_FINALIZATION_ONLY', 'TYCHE_ACTIVE_MODEL_RECEIPT']
    forwarded += ['TYCHE_WORKER_ID', 'TYCHE_WORKER_GENERATION', 'TYCHE_BUDGET_POLICY']
    return ('\n[mcp_servers.tyche]\ncommand = ' + json.dumps(sys.executable) + '\nargs = ' + json.dumps(args) + '\n'
            'env_vars = ' + json.dumps(forwarded) + '\n'
            'cwd = ' + json.dumps(str(ROOT)) + '\nrequired = true\n'
            'startup_timeout_sec = 40\ntool_timeout_sec = 900\n'
            'default_tools_approval_mode = "approve"\n')


def inspect_runtime(env, overrides, start_thread=False, native_tools=False):
    """Use the installed runtime's discovery results, not a filesystem guess."""
    messages = queue.Queue()
    with tempfile.TemporaryFile(mode='w+') as errors:
        proc = subprocess.Popen(
            [env.get('TYCHE_CODEX_BINARY', 'codex'), 'app-server', '--stdio', *overrides], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, bufsize=1,
        )

        def read_messages():
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass

        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()

        def request(ident, method, params):
            proc.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    message = messages.get(timeout=1)
                except queue.Empty:
                    if proc.poll() is not None:
                        raise RuntimeError('Codex app-server could not start; run codex doctor.')
                    continue
                if message.get('id') == ident:
                    if 'error' in message:
                        raise RuntimeError(f'{method}: {message["error"]["message"]}')
                    return message['result']
            raise RuntimeError(f'Codex timed out during {method}; no test was launched.')

        try:
            request(1, 'initialize', {
                'clientInfo': {'name': 'tyche_isolation_check', 'version': '1.0'},
                'capabilities': {'experimentalApi': True},
            })
            proc.stdin.write('{"method":"initialized"}\n')
            proc.stdin.flush()
            listed = request(2, 'skills/list', {'cwds': [str(ROOT)], 'forceReload': True})
            if any(row.get('errors') for row in listed['data']):
                raise RuntimeError('Codex reported skill discovery errors; no test was launched.')
            skills = [
                {key: skill.get(key) for key in ('name', 'path', 'scope', 'enabled')}
                for row in listed['data'] for skill in row['skills']
            ]
            result = {'skills': skills}
            if start_thread:
                # Inherit the same project sandbox/network configuration as
                # the real execution. A read-only override hid proxy startup
                # failures that occurred only when sourcing enabled networking.
                started = request(3, 'thread/start', {
                    'cwd': str(ROOT), 'ephemeral': True,
                })
                if 'instructionSources' not in started:
                    raise RuntimeError('This Codex version cannot report loaded instruction sources.')
                result['instruction_sources'] = started['instructionSources']
                result['model'] = started.get('model')
                result['native_tools'] = []
                if native_tools:
                    servers = request(4, 'mcpServerStatus/list', {'limit': 100})
                    tyche = next((r for r in servers.get('data', []) if r.get('name') == 'tyche'), None)
                    if tyche is None or len(tyche.get('tools', {})) != 6:
                        raise RuntimeError('TYCHE native tools did not initialize; no model turn or provider call was started.')
                    result['native_tools'] = list(tyche['tools'])
            return result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            reader.join(timeout=1)
            proc.stdin.close()
            proc.stdout.close()


def smoke(command, env):
    """A model exit code alone cannot prove the native tool actually worked."""
    completed = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120)
    print(completed.stdout, end='', flush=True)
    print(completed.stderr, end='', file=sys.stderr, flush=True)
    calls = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
            item = event.get('item', {}) if isinstance(event, dict) else {}
            if event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call' and item.get('server') == 'tyche':
                calls.append(item)
        except (ValueError, AttributeError):
            continue
    if (completed.returncode or len(calls) != 2 or any(call.get('tool') != 'tyche_inspect'
            or call.get('status') != 'completed' for call in calls)):
        raise RuntimeError('Read-only native tool smoke test failed; no sourcing run was started.')
    for call in calls:
        try:
            result = call['result']
            payload = json.loads(result['content'][0]['text'])
            healthy = not call.get('error') and not result.get('isError') and payload.get('status') == 'not_started'
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            healthy = False
        if not healthy:
            raise RuntimeError('Read-only native tool smoke test failed; no sourcing run was started.')
    return 0


def main():
    launched_at = datetime.now(timezone.utc).isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Verify isolation and sourcing-session startup, including network setup; no model turn or provider calls.')
    mode.add_argument('--smoke', action='store_true', help='Run a read-only model test; no provider calls.')
    mode.add_argument('--exec', action='store_true', help='Run the supplied prompt noninteractively.')
    mode.add_argument('--exec-file', type=Path, help='Read the exact request from a UTF-8 file and run it noninteractively.')
    parser.add_argument('--resume-until', help='Explicit user-authorized research deadline (ISO timestamp); preserves the original clock and budget.')
    parser.add_argument('--resume-reason', help='Record the user instruction authorizing this extension.')
    parser.add_argument('prompt', nargs='?', help='Sourcing request with explicit scope and budget.')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=DEFAULT_WORKERS,
                        help='Parallel researchers sharing one run (default: 2); applies to --exec-file.')
    parser.add_argument('--budget-policy', choices=('actual_cost', 'reserved'), default=None,
                        help='Accounting for new runs; reserved preserves a hard provider-only cap for comparisons. Resumes retain their saved policy.')
    args = parser.parse_args()
    if (args.resume_until is not None or args.resume_reason is not None) and not (
            args.exec_file is not None and args.resume_until and args.resume_reason):
        parser.error('--resume-until and --resume-reason require each other and --exec-file')
    if args.exec and not args.prompt:
        parser.error('--exec requires a prompt')
    if (args.check or args.smoke) and args.prompt:
        parser.error('--check and --smoke do not accept a prompt')
    if args.exec_file is not None:
        if args.prompt is not None:
            parser.error('--exec-file does not accept an additional prompt')
        args.prompt = args.exec_file.read_text(encoding='utf-8')
        if not args.prompt.strip():
            parser.error('the request file is empty')
    if os.environ.get('TYCHE_ISOLATED_RUN') == '1':
        raise RuntimeError('Already inside isolated TYCHE. Use the local lead-sourcing skill directly; nested launch refused.')

    source_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
    # Give CODEX_HOME its documented meaning only in the child process. The
    # parent environment and the user's global instruction/config files stay intact.
    with tempfile.TemporaryDirectory(prefix='tyche-codex-home-') as profile:
        tyche_codex_home = Path(profile)
        native_tools = args.exec_file is not None or args.check or args.smoke
        tool_run_file = (args.exec_file.resolve().parent / 'results.json' if args.exec_file is not None
                         else tyche_codex_home / 'tool-check/results.json')
        (tyche_codex_home / 'config.toml').write_text(
            '[projects.' + json.dumps(str(ROOT)) + ']\ntrust_level = "trusted"\n'
            + (tool_configuration(tool_run_file, readonly=args.check or args.smoke) if native_tools else '')
        )
        for name in ('auth.json', 'rules'):
            source = source_home / name
            if source.exists():
                (tyche_codex_home / name).symlink_to(source, target_is_directory=source.is_dir())
        # Research uses the installed CLI and project skill. CLI self-updates
        # and global skill sync can contact npm or alter context mid-run.
        env = dict(os.environ, CODEX_HOME=profile, TYCHE_ISOLATED_RUN='1',
                   DEEPLINE_NO_AUTO_UPDATE='1', DEEPLINE_SKIP_SKILLS_SYNC='1',
                   TYCHE_RUN_STARTED_AT=launched_at)
        env = workspace_environment(env)
        if args.budget_policy:
            env['TYCHE_BUDGET_POLICY'] = args.budget_policy
        env['TYCHE_CODEX_BINARY'] = codex_binary(env)
        if args.exec_file is not None:
            env['TYCHE_REQUEST_FILE'] = str(args.exec_file.resolve())
            for key in ('TYCHE_WORKSPACE_NODE', 'TYCHE_WORKSPACE_PYTHON'):
                if not Path(env.get(key, '')).is_file():
                    raise RuntimeError(f'Configure {key} before starting a sourcing run')
            if not (Path(env.get('TYCHE_WORKSPACE_NODE_MODULES', '')) / '@oai/artifact-tool/package.json').is_file():
                raise RuntimeError('Configure TYCHE_WORKSPACE_NODE_MODULES before starting a sourcing run')
        # Pin the isolated runner's model selection instead of inheriting the
        # user's current Codex default. Keep the repository's Luna/high/Fast
        # selection consistent for isolated sourcing.
        overrides = ['-c', 'model=' + json.dumps(MODEL),
                     '-c', 'model_reasoning_effort=' + json.dumps(REASONING_EFFORT),
                     '-c', 'service_tier=' + json.dumps(SERVICE_TIER)]
        for feature in ('plugins', 'apps', 'memories', 'hooks', 'shell_snapshot'):
            overrides.extend(['--disable', feature])
        overrides.extend(['-c', 'developer_instructions=' + json.dumps(ISOLATION_INSTRUCTIONS)])

        discovered = inspect_runtime(env, overrides)
        excluded = [skill['path'] for skill in discovered['skills']
                    if not inside(skill['path'], SKILL_ROOT)]
        overrides.extend(['-c', 'skills.config=[' + ','.join(
            '{path=' + json.dumps(path) + ',enabled=false}' for path in excluded
        ) + ']'])
        verified = inspect_runtime(env, overrides, start_thread=True, native_tools=native_tools)
        active = [skill for skill in verified['skills'] if skill['enabled']]
        if not active or any(not inside(skill['path'], SKILL_ROOT) for skill in active):
            raise RuntimeError('Skill isolation failed; no test was launched.')
        if any(not inside(path, ROOT) for path in verified['instruction_sources']):
            raise RuntimeError('External instructions were loaded; no test was launched.')
        summary = {
            'isolation_passed': True, 'cwd': str(ROOT),
            'instruction_sources': verified['instruction_sources'],
            'enabled_skills': active, 'disabled_external_skills': len(excluded),
            'plugins_enabled': False, 'apps_enabled': False, 'memories_enabled': False,
            'codex_version': CODEX_VERSION, 'model': verified['model'], 'reasoning_effort': REASONING_EFFORT,
            'service_tier': SERVICE_TIER,
            'native_tools': verified['native_tools'],
        }
        print(json.dumps(summary, indent=2), flush=True)
        if args.check:
            return 0
        if not (source_home / 'auth.json').is_file():
            raise RuntimeError('No reusable file-based Codex login. Run codex login first, or use a separately authenticated profile.')

        if args.smoke or args.exec or args.exec_file is not None:
            # File-based runs retain a journal only inside this temporary
            # profile, long enough to capture numeric per-response usage.
            command = [env['TYCHE_CODEX_BINARY'], 'exec', *([] if args.exec_file is not None else ['--ephemeral']), *overrides]
            if args.exec_file is not None or args.smoke:
                command.append('--json')
            if args.smoke:
                command.extend(['--sandbox', 'read-only', '-c', 'web_search="disabled"',
                                '-c', 'sandbox_workspace_write.network_access=false',
                                '--disable', 'unbounded_connection_retries'])
            command.append(SMOKE_PROMPT if args.smoke else args.prompt)
        else:
            command = [env['TYCHE_CODEX_BINARY'], *overrides]
            if args.prompt:
                command.append(args.prompt)
        try:
            if args.smoke:
                return smoke(command, env)
            if args.exec_file is not None:
                if args.resume_until:
                    authorize_resume(args.exec_file, args.resume_until, args.resume_reason)
                env["TYCHE_PARALLEL_WORKERS"] = str(args.workers)
                return supervise_worker(command, args.exec_file, env, tyche_codex_home,
                                        resume=bool(args.resume_until))
            return subprocess.call(
                command, cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL if args.smoke or args.exec or args.exec_file is not None else None,
                timeout=120 if args.smoke else None,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError('The read-only smoke test exceeded 120 seconds and was stopped.')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'TYCHE isolation: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
