"""Run a fixed pool of identical researchers on the same saved sourcing loop."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from pathlib import Path
import threading
import time


def _billing_pending(progress):
    """Recognize the exact saved stop reason without racing route persistence."""
    return (progress.get("stop") == "input_or_configuration_stop"
            and progress.get("stop_reason") == "billing_pending")


def run_research(command, request_file, env, profile, count=2, *, host=None):
    try:
        from . import codex_tyche as launcher
    except ImportError:
        import codex_tyche as launcher
    import run_coordination as coordination
    from research_tools import ResearchTools
    from run_attempt import recover_completed_attempts
    from validate_run import DELIVERY_STOPS, _company_key
    host = host or launcher.LocalHost()

    request_file = Path(request_file).resolve()
    run_file = request_file.parent / "results.json"
    coordination.configure(run_file, count)
    startup_until = time.time() + launcher.STARTUP_SECONDS
    stopped = threading.Event()
    active, failures, attempts = {}, {}, {}
    reason = None
    billing_drain = False
    # This function is entered only after the prior research pool was joined.
    def reopen(value):
        value["phase"] = "research"
        if run_file.exists():
            value["ready"] = True
            document = json.loads(run_file.read_text())
            outcomes = {_company_key(row): status for status in ("accepted", "rejected")
                        for row in document.get(status, [])}
            for key, claim in value["claims"].items():
                claim["status"] = outcomes.get(key, "active")
    coordination.update(run_file, reopen)
    if run_file.exists():
        recovery = recover_completed_attempts(run_file)
        if recovery["errors"]:
            raise ValueError("Saved dispatch accounting needs recovery: " + json.dumps(recovery))

    def deadline():
        if stopped.is_set():
            return time.time()
        return launcher.research_deadline(request_file, env["TYCHE_RUN_STARTED_AT"]) if run_file.exists() else startup_until

    def observe_progress():
        progress = ResearchTools(run_file, environment=env)._overview()
        pending_billing = _billing_pending(progress)
        if pending_billing:
            # Match the serial runner: recover attributable bills without a
            # paid retry, and let active model turns close their usage records.
            try:
                host.reconcile_research(run_file)
            except (OSError, RuntimeError, ValueError):
                # Preserve the pending stop. The supervisor retries/blocks
                # after model turns have closed; never kill them for a read failure.
                pass
            progress = ResearchTools(run_file, environment=env)._overview()
            pending_billing = _billing_pending(progress)
        return progress, pending_billing

    def invoke(worker, *, take_serial=False):
        with coordination.locked(run_file):
            receipt = host.research_receipt(request_file)
            receipt.data.update(worker_id=worker, phase="research", run_started_at=env["TYCHE_RUN_STARTED_AT"])
            receipt.save()
            coordination.register(run_file, worker, receipt.path.stem)
            if take_serial:
                coordination.update(run_file, lambda value: value.update(serial_worker=worker))
        worker_env = dict(env, TYCHE_WORKER_ID=worker, TYCHE_WORKER_GENERATION=receipt.path.stem,
                          TYCHE_FINALIZATION_ONLY="0")
        position = int(worker.rsplit("-", 1)[1])
        task = ("Continue the saved request and current evidence in this run. Historical review feedback is not a verdict: check whether newer evidence has resolved it."
                if attempts.get(worker) else command[-1])
        prompt = (task + "\n\nParallel run instructions: You are " + worker + " of " + str(count) + ". "
            "All workers run the same discovery, company qualification and contact-enrichment loop. "
            "Interpret the ICP's distinct search approaches in the order given; start with approach " + str(position) +
            ", or a different query/source if fewer approaches exist. Vary approaches when needed, without changing criteria. "
            + ("The request is initialized; use tyche_inspect and its saved interpretation. " if run_file.exists() else
               "Worker-1 initializes the request once with tyche_start; other workers use tyche_inspect and the saved interpretation. ") +
            "Use tyche_claim with a real website domain BEFORE company-specific research; include its verified LinkedIn company_url when known. "
            "If claimed=false, skip that company. Use its returned target for subsequent calls. "
            "Work ONE company at a time through the existing workflow: find, claim, qualify, then complete contacts and confirm the lead. "
            "Finish that company before claiming another or doing broad discovery. Only enrich passing accounts. "
            "Reject evidenced mismatches; if genuinely blocked, save hold_account/hold_contact with the specific missing evidence "
            "and why available routes cannot resolve it before moving on. Do not hold simply to open more candidates. "
            "Use company-scoped searches to resolve the current candidate. Resume parallel.current_company first after a restart. "
            "Review your own sources and companies as you go. Saved parallel.owned_companies lists your work after restart. "
            "Do not edit shared files or invoke diagnostic CLIs in ordinary research. All provider work uses native tools. "
            "All workers share one budget, target, deadline and evidence standard; never start a separate run. "
            "Code checks spending automatically. While permitted, focus on completing good leads, not repeated cost inspections. "
            "Near the limit, finish your current company. A worker_yield response means end this invocation immediately, without polling. "
            "If stop=continue, do useful research. If stop is terminal, save your current judgments and end; "
            "the supervisor waits for all researchers and runs one final review/export. Never spawn additional workers.")
        worker_command = list(command)
        worker_command[-1] = prompt
        attempts[worker] = attempts.get(worker, 0) + 1
        print(json.dumps({"worker": worker, "attempt": attempts[worker], "phase": "research",
                          "model_usage_receipt": str(receipt.path)}), flush=True)
        try:
            logs = request_file.parent / "worker-logs"
            logs.mkdir(exist_ok=True)
            with (logs / (receipt.path.stem + ".jsonl")).open("w", encoding="utf-8") as output:
                host.execute_research(worker_command, request_file, worker_env, receipt,
                    profile=profile, deadline=deadline, output=output,
                    cost_stop=lambda: (reason or "pool_stopped") if stopped.is_set() else launcher.cost_stop(request_file, receipt.path.stem))
        except (OSError, RuntimeError) as exc:
            # execute_with_usage reaps its owned process group before returning or raising.
            # Shared cleanup/state/accounting failures still stop the pool below.
            receipt.data.update(failure_kind="worker_error", failure_detail=type(exc).__name__, exit_code=1)
            if not receipt.data.get("finished_at"):
                receipt.finish(1)
            else:
                receipt.save()
        finally:
            def ended(value):
                current = value.get("workers", {}).get(worker, {})
                if current.get("generation") == receipt.path.stem:
                    current.update(status="stopped", last_usage_receipt=receipt.path.name)
            coordination.update(run_file, ended)
        return receipt.data

    pool = ThreadPoolExecutor(max_workers=count)
    drain_until = None
    last_progress = 0
    try:
        state = coordination.snapshot(run_file)
        eligible = [f"worker-{i}" for i in range(1, count + 1)
                    if not state["workers"].get(f"worker-{i}", {}).get("disabled")]
        if not eligible:
            raise RuntimeError("No healthy research worker remains; preserve the saved failures")
        first = state.get("serial_worker") if state.get("serial_worker") in eligible else eligible[0]
        if state.get("serial_worker") and state["serial_worker"] != first:
            coordination.update(run_file, lambda value: value.update(serial_worker=first))
        active[pool.submit(invoke, first)] = first
        while active:
            state = coordination.snapshot(run_file)
            if state["ready"]:
                coordination.refresh_pacing(run_file, reconcile=host.reconcile_research)
                state = coordination.snapshot(run_file)
            progress, pending_billing = observe_progress() if state["ready"] else ({}, False)
            if billing_drain and not pending_billing and not stopped.is_set():
                # A posted bill may restore normal work while a peer is still
                # running. Cancel the old billing timer before restarting work.
                drain_until = None
            if not stopped.is_set():
                billing_drain = pending_billing
            if time.monotonic() - last_progress >= 30:
                print(json.dumps({"parallel_progress": {"workers": state["workers"],
                    "claimed_companies": len(state["claims"]), "duplicate_claims_prevented": state["conflicts"],
                    "summary": progress.get("summary"), "stop": progress.get("stop")}}), flush=True)
                last_progress = time.monotonic()
            limit = launcher.research_deadline(request_file, env["TYCHE_RUN_STARTED_AT"])
            stop = progress.get("stop")
            fatal = progress.get("operational_block") or (
                stop if stop in {"provider_stop", "input_or_configuration_stop"} and not pending_billing else None)
            if not state["ready"]:
                status_path = request_file.parent / "operational-status.json"
                if status_path.exists():
                    status = json.loads(status_path.read_text())
                    if status.get("status") == "operationally_blocked":
                        fatal = status.get("reason", "Run setup is blocked")
            terminal = billing_drain or stop in DELIVERY_STOPS or limit is not None and time.time() >= limit
            if fatal:
                reason = str(fatal)
                stopped.set()
            elif terminal and drain_until is None:
                # Tools refuse new work at the shared stop. Give researchers a
                # bounded chance to save the judgments they already possess.
                drain_until = time.time() + 45
            if drain_until is not None and time.time() >= drain_until:
                stopped.set()
            if state["ready"] and not terminal and not stopped.is_set():
                for index in range(1, count + 1):
                    worker = f"worker-{index}"
                    if worker not in active.values() and not state.get("serial_worker") and not state["workers"].get(worker, {}).get("disabled"):
                        active[pool.submit(invoke, worker)] = worker
            done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
            if done and state["ready"]:
                current, pending_billing = observe_progress()
                billing_drain = billing_drain or pending_billing
                terminal = terminal or billing_drain or current.get("stop") in DELIVERY_STOPS
                fatal = current.get("operational_block") or (
                    current.get("stop") if current.get("stop") in {"provider_stop", "input_or_configuration_stop"}
                    and not pending_billing else None)
                if fatal:
                    reason = str(fatal)
                    stopped.set()
            for future in done:
                worker = active.pop(future)
                try:
                    data = future.result()
                except Exception as exc:
                    # Local model transport errors were handled after cleanup in invoke.
                    # An escaping lifecycle/receipt/registry failure is shared state: fail closed.
                    raise RuntimeError(f"worker_lifecycle_state_failure: {worker} ({type(exc).__name__})") from exc
                failure = data.get("failure_kind")
                if not coordination.snapshot(run_file)["ready"]:
                    reason = "run_not_initialized"
                    stopped.set()  # No authoritative budget exists for an automatic retry.
                if data.get("cleanup_error") or failure in {"cancelled", "model_usage_limit", "invalid_saved_state", "host_limit"}:
                    reason = data.get("cleanup_error") or failure
                    stopped.set()
                failures[worker] = failures.get(worker, 0) + 1 if (data.get("exit_code") or data.get("status") != "complete") and failure != "deadline_reached" else 0
                if failures[worker] >= 2:
                    if not state["ready"]:
                        reason = "repeated_worker_failure: " + worker
                        stopped.set()
                    else:
                        coordination.update(run_file, lambda value: value["workers"][worker].update(
                            disabled=True, failure="repeated_worker_failure"))
                latest = coordination.snapshot(run_file)
                if not terminal and not stopped.is_set() and not latest["workers"][worker].get("disabled") and not coordination.should_yield(latest, worker):
                    # The same slot retains its companies. Never give a live
                    # worker's claims to another slot or replay a paid request.
                    active[pool.submit(invoke, worker)] = worker
            if not active and not reason:
                latest = coordination.snapshot(run_file)
                serial = latest.get("serial_worker")
                standby = [f"worker-{i}" for i in range(1, count + 1)
                           if not latest["workers"].get(f"worker-{i}", {}).get("disabled")]
                if not terminal and serial and latest["workers"][serial].get("disabled") and standby:
                    # A peer may already have yielded at the budget boundary.
                    # Let that healthy slot take over when the serial owner retires.
                    replacement = min(standby)
                    active[pool.submit(invoke, replacement, take_serial=True)] = replacement
                    continue
                if not terminal:
                    reason = "No healthy research worker remains"
                break
    except BaseException as exc:
        reason = "cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
        stopped.set()
        raise
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
        if run_file.exists():
            host.research_report(request_file.parent)
        coordination.update(run_file, lambda value: value.update(phase="blocked" if reason else "finalization"))
    if reason:
        raise RuntimeError(reason)
    if not run_file.exists():
        raise RuntimeError("Worker initialization did not create a saved run; no research was delivered")
    recovery = recover_completed_attempts(run_file)
    if recovery["errors"]:
        raise ValueError("Parallel research stopped with unresolved dispatch accounting: " + json.dumps(recovery))
