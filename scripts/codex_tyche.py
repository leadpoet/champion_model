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

from run_costs import UsageReceipt, execute_with_usage, save_report


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / '.agents' / 'skills'
sys.path.insert(0, str(SKILL_ROOT / 'lead-sourcing' / 'scripts'))
MODEL = 'gpt-5.6-luna'
REASONING_EFFORT = 'xhigh'
SERVICE_TIER = 'fast'
FINALIZATION_SECONDS = 600


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
    from validate_run import DEFAULT_MAX_DURATION_SECONDS, run_deadline
    document = saved_run(request_file)
    if document is None:
        return datetime.fromisoformat(started_at.replace('Z', '+00:00')).timestamp() + DEFAULT_MAX_DURATION_SECONDS
    limit = run_deadline(document)
    return limit.timestamp() if limit is not None else None


def supervise_worker(command, request_file, env, profile):
    """Continue saved research, not paid calls. Completion is a checked artifact."""
    try:
        return _supervise_worker(command, request_file, env, profile)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
            'reason': 'invalid_or_unavailable_runtime_state', 'detail': str(exc)[:2000],
            'resume': 'Repair the saved state or runtime; preserve the original clock, ledger and receipts.'})
        print('TYCHE runtime blocked: ' + str(exc), file=sys.stderr)
        return 1


def _supervise_worker(command, request_file, env, profile):
    from research_tools import ResearchTools
    from run_attempt import recover_completed_attempts
    from validate_run import DELIVERY_STOPS
    request_file = Path(request_file).resolve()
    run_file = request_file.parent / 'results.json'
    env = dict(env, TYCHE_RUN_STARTED_AT=original_start(request_file, env['TYCHE_RUN_STARTED_AT']))
    finishing_until = None
    failed_exits = 0
    attempt = 0
    continuation = ('Current invocation request and review feedback:\n' + command[-1] + '\n\n'
        'Continuation controls: Continue the SAME saved run at ' + str(run_file) + '. '
        'Read the local skill. Preserve the saved request, start time, ledger, '
        'reservations and evidence. Recover saved responses; never replay an uncertain paid call. '
        'An empty queue or exhausted search approach requires a different strategy, not completion. ')
    while True:
        document = saved_run(request_file)
        if document is not None:
            recovery = recover_completed_attempts(run_file)
            if recovery['errors']:
                write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
                    'reason': 'saved_dispatch_accounting_incomplete', 'run_file': str(run_file),
                    'recovery': recovery,
                    'resume': 'Preserve the original clock, ledger and receipts. Resume after saved responses or billing evidence reconcile the pending calls; never replay paid requests.'})
                return 1
        progress = ResearchTools(run_file, environment=env)._overview() if document is not None else {}
        limit = research_deadline(request_file, env['TYCHE_RUN_STARTED_AT'])
        stop = progress.get('stop')
        terminal = (stop in DELIVERY_STOPS or (limit is not None and time.time() >= limit)
                    or (finishing_until is not None and stop != 'continue'))
        blocked = progress.get('operational_block') or (
            stop if stop in {'provider_stop', 'input_or_configuration_stop'} else None)
        if blocked:
            status = {'status': 'blocked', 'delivery_allowed': False, 'reason': str(blocked), 'run_file': str(run_file)}
            write_worker_status(request_file, status)
            return 1
        if not terminal:
            # A semantic review may demote a row after reaching the target.
            # Re-evaluate the saved clock/budget; finalization is not a new stop reason.
            finishing_until = None
        elif finishing_until is None:
            # Review may use the original remaining time. The short grace is
            # for runs at their deadline, not an earlier cap on a valid repair.
            finishing_until = max(time.time(), limit or 0) + FINALIZATION_SECONDS
        if finishing_until is not None and time.time() >= finishing_until:
            write_worker_status(request_file, {'status': 'blocked', 'delivery_allowed': False,
                'reason': 'finalization_timeout', 'run_file': str(run_file),
                'resume': 'Review/export saved evidence only; do not reopen sourcing or reset accounting.'})
            return 1
        # Research and final review use separate contexts, sharing the same run.
        worker_env = dict(env, TYCHE_FINALIZATION_ONLY='1' if terminal else '0')
        worker_command = list(command)
        if attempt or terminal:
            worker_command[-1] = continuation + ('Research has stopped. Request the final evidence packet with '
                'tyche_finish before individual field inspections, then follow its review instructions. '
                'If corrections leave the target incomplete, save them and return; the supervisor will '
                're-evaluate remaining time and budget before allowing more research.' if terminal else
                'Use tyche_inspect first. Continue useful sourcing while budget and time remain, then review and deliver.')
        receipt = UsageReceipt(request_file, MODEL, REASONING_EFFORT, SERVICE_TIER)
        receipt.data['run_started_at'] = env['TYCHE_RUN_STARTED_AT']
        receipt.save()
        print(json.dumps({'model_usage_receipt': str(receipt.path), 'attempt': attempt + 1,
                          'phase': 'finalization' if terminal else 'research'}), flush=True)
        try:
            execute_with_usage(worker_command, ROOT, worker_env, receipt, profile=profile,
                deadline=(lambda: finishing_until) if terminal else
                         (lambda: research_deadline(request_file, env['TYCHE_RUN_STARTED_AT'])))
        finally:
            status_path = close_worker(request_file, receipt, worker_env)
            print(json.dumps({'worker_status': str(status_path),
                              'run_cost_report': str(save_report(request_file.parent))}), flush=True)
        status = json.loads(status_path.read_text())
        if status['delivery_allowed']:
            return 0 if receipt.data['status'] == 'complete' else 2
        failure = receipt.data.get('failure_kind')
        if receipt.data.get('cleanup_error') or failure in {'cancelled', 'model_usage_limit', 'invalid_saved_state'}:
            status.update(status='blocked', reason=receipt.data.get('cleanup_error') or failure)
            write_worker_status(request_file, status)
            return 1
        failed_exits = failed_exits + 1 if receipt.data.get('exit_code') and failure != 'deadline_reached' else 0
        if failed_exits >= 2:
            status.update(status='blocked', reason='repeated_worker_failure')
            write_worker_status(request_file, status)
            return 1
        attempt += 1
        print(json.dumps({'continuing_saved_run': str(run_file), 'reason': failure or 'premature_worker_exit'}), flush=True)


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
    status = {'status': 'interrupted', 'delivery_allowed': False, 'run_file': str(path),
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
            status.update(status='complete', delivery_allowed=True, reason='verified_saved_workbook')
        elif not reviewed and not receipt.data.get('failure_kind'):
            status.update(status='review_required' if status['accepted_count'] == status['target_count'] else 'incomplete',
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
                 'TYCHE_RUN_STARTED_AT', 'TYCHE_REQUEST_FILE', 'TYCHE_FINALIZATION_ONLY']
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
            ['codex', 'app-server', '--stdio', *overrides], cwd=ROOT, env=env,
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
                    if tyche is None or len(tyche.get('tools', {})) != 5:
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
    parser.add_argument('prompt', nargs='?', help='Sourcing request with explicit scope and budget.')
    args = parser.parse_args()
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
        if args.exec_file is not None:
            env['TYCHE_REQUEST_FILE'] = str(args.exec_file.resolve())
            for key in ('TYCHE_WORKSPACE_NODE', 'TYCHE_WORKSPACE_PYTHON'):
                if not Path(env.get(key, '')).is_file():
                    raise RuntimeError(f'Configure {key} before starting a sourcing run')
            if not (Path(env.get('TYCHE_WORKSPACE_NODE_MODULES', '')) / '@oai/artifact-tool/package.json').is_file():
                raise RuntimeError('Configure TYCHE_WORKSPACE_NODE_MODULES before starting a sourcing run')
        # Pin the isolated runner's model selection instead of inheriting the
        # user's current Codex default.  `xhigh` is the UI's Extra High effort;
        # `fast` selects the accelerated service tier when available.
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
            'model': verified['model'], 'reasoning_effort': REASONING_EFFORT,
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
            command = ['codex', 'exec', *([] if args.exec_file is not None else ['--ephemeral']), *overrides]
            if args.exec_file is not None or args.smoke:
                command.append('--json')
            if args.smoke:
                command.extend(['--sandbox', 'read-only', '-c', 'web_search="disabled"',
                                '-c', 'sandbox_workspace_write.network_access=false',
                                '--disable', 'unbounded_connection_retries'])
            command.append(SMOKE_PROMPT if args.smoke else args.prompt)
        else:
            command = ['codex', *overrides]
            if args.prompt:
                command.append(args.prompt)
        try:
            if args.smoke:
                return smoke(command, env)
            if args.exec_file is not None:
                return supervise_worker(command, args.exec_file, env, tyche_codex_home)
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
    except (RuntimeError, OSError) as exc:
        print(f'TYCHE isolation: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
