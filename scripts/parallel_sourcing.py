"""Run a fixed pool of identical researchers on the same saved sourcing loop."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from pathlib import Path
import threading
import time


def run_research(command, request_file, env, profile, count=3):
    import codex_tyche as launcher
    import run_coordination as coordination
    from research_tools import ResearchTools
    from run_attempt import recover_completed_attempts
    from run_costs import UsageReceipt, execute_with_usage, save_report
    from validate_run import DELIVERY_STOPS

    request_file = Path(request_file).resolve()
    run_file = request_file.parent / "results.json"
    coordination.configure(run_file, count)
    stopped = threading.Event()
    active, failures, attempts = {}, {}, {}
    reason = None
    state = coordination.snapshot(run_file)
    # This function is entered only after the prior research pool was joined.
    coordination.update(run_file, lambda value: value.update(phase="research"))
    if run_file.exists():
        recovery = recover_completed_attempts(run_file)
        if recovery["errors"]:
            raise ValueError("Saved dispatch accounting needs recovery: " + json.dumps(recovery))

    def deadline():
        if stopped.is_set():
            return time.time()
        return launcher.research_deadline(request_file, env["TYCHE_RUN_STARTED_AT"])

    def invoke(worker):
        receipt = UsageReceipt(request_file, launcher.MODEL, launcher.REASONING_EFFORT, launcher.SERVICE_TIER)
        receipt.data.update(worker_id=worker, phase="research", run_started_at=env["TYCHE_RUN_STARTED_AT"])
        receipt.save()
        coordination.register(run_file, worker, receipt.path.stem)
        worker_env = dict(env, TYCHE_WORKER_ID=worker, TYCHE_WORKER_GENERATION=receipt.path.stem,
                          TYCHE_FINALIZATION_ONLY="0")
        position = int(worker.rsplit("-", 1)[1])
        prompt = (command[-1] + "\n\nParallel run instructions: You are " + worker + " of " + str(count) + ". "
            "All workers run the same discovery, company qualification and contact-enrichment loop. "
            "Interpret the ICP's distinct search approaches in the order given; start with approach " + str(position) +
            ", or a different query/source if fewer approaches exist. Vary approaches when needed, without changing criteria. "
            "Worker-1 initializes the request once with tyche_start; other workers use tyche_inspect and the saved interpretation. "
            "Use tyche_claim with a real website domain BEFORE company-specific research; include its verified LinkedIn company_url when known. "
            "If claimed=false, skip that company. Use its returned target for subsequent calls. "
            "Work each owned company through account qualification and then contacts. Only enrich passing accounts. "
            "Review your own sources and companies as you go. Saved parallel.owned_companies lists your work after restart. "
            "Do not edit shared files or invoke diagnostic CLIs in ordinary research. All provider work uses native tools. "
            "All workers share one budget, target, deadline and evidence standard; never start a separate run. "
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
                execute_with_usage(worker_command, launcher.ROOT, worker_env, receipt,
                                   profile=profile, deadline=deadline, output=output)
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
        active[pool.submit(invoke, "worker-1")] = "worker-1"
        launched = {"worker-1"}
        while active:
            state = coordination.snapshot(run_file)
            progress = ResearchTools(run_file, environment=env)._overview() if state["ready"] else {}
            if time.monotonic() - last_progress >= 30:
                print(json.dumps({"parallel_progress": {"workers": state["workers"],
                    "claimed_companies": len(state["claims"]), "duplicate_claims_prevented": state["conflicts"],
                    "summary": progress.get("summary"), "stop": progress.get("stop")}}), flush=True)
                last_progress = time.monotonic()
            limit = launcher.research_deadline(request_file, env["TYCHE_RUN_STARTED_AT"])
            stop = progress.get("stop")
            fatal = progress.get("operational_block") or (stop if stop in {"provider_stop", "input_or_configuration_stop"} else None)
            if not state["ready"]:
                status_path = request_file.parent / "operational-status.json"
                if status_path.exists():
                    status = json.loads(status_path.read_text())
                    if status.get("status") == "operationally_blocked":
                        fatal = status.get("reason", "Run setup is blocked")
            terminal = stop in DELIVERY_STOPS or limit is not None and time.time() >= limit
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
                for index in range(2, count + 1):
                    worker = f"worker-{index}"
                    if worker not in launched:
                        active[pool.submit(invoke, worker)] = worker
                        launched.add(worker)
            done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
            if done and state["ready"]:
                current = ResearchTools(run_file, environment=env)._overview()
                terminal = terminal or current.get("stop") in DELIVERY_STOPS
            for future in done:
                worker = active.pop(future)
                data = future.result()
                failure = data.get("failure_kind")
                if data.get("cleanup_error") or failure in {"cancelled", "model_usage_limit", "invalid_saved_state"}:
                    reason = data.get("cleanup_error") or failure
                    stopped.set()
                failures[worker] = failures.get(worker, 0) + 1 if data.get("exit_code") and failure != "deadline_reached" else 0
                if failures[worker] >= 2:
                    reason = "repeated_worker_failure: " + worker
                    stopped.set()
                if not terminal and not stopped.is_set():
                    # The same slot retains its companies. Never give a live
                    # worker's claims to another slot or replay a paid request.
                    active[pool.submit(invoke, worker)] = worker
            if not active and not reason:
                break
    except BaseException as exc:
        reason = "cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
        stopped.set()
        raise
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
        if run_file.exists():
            save_report(request_file.parent)
        coordination.update(run_file, lambda value: value.update(phase="blocked" if reason else "finalization"))
    if reason:
        raise RuntimeError(reason)
    if not run_file.exists():
        raise RuntimeError("Worker initialization did not create a saved run; no research was delivered")
    recovery = recover_completed_attempts(run_file)
    if recovery["errors"]:
        raise ValueError("Parallel research stopped with unresolved dispatch accounting: " + json.dumps(recovery))


def supervise(command, request_file, env, profile, count=3):
    """The existing supervisor remains the sole finalization and delivery owner."""
    import codex_tyche as launcher
    request_file = Path(request_file).resolve()
    env = dict(env, TYCHE_RUN_STARTED_AT=launcher.original_start(request_file, env["TYCHE_RUN_STARTED_AT"]),
               TYCHE_PARALLEL_WORKERS=str(count))
    return launcher.supervise_worker(command, request_file, env, profile)
