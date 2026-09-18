"""Small, local coordination layer for workers sharing one sourcing run.

OS locks serialize short state changes and limit concurrent provider calls.
They do not replace receipt recovery or expire company ownership on a timer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import urlsplit


_locks = {}
_registry_lock = threading.Lock()
_held = threading.local()
_worker_context = ContextVar("tyche_worker", default=None)


@contextmanager
def worker_context(run_file, worker, generation):
    token = _worker_context.set((run_file, worker, generation) if worker else None)
    try:
        yield
    finally:
        _worker_context.reset(token)


def check_current_worker():
    current = _worker_context.get()
    if current:
        run_file, worker, generation = current
        check_worker(snapshot(run_file), worker, generation)


def _os_lock(fd, blocking=True):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _unlock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Coordination lock must be a regular file")
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    return fd


@contextmanager
def locked(run_file, name="state"):
    """Reentrant across local threads, exclusive across worker processes."""
    directory = Path(run_file).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (".tyche-" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".guard")
    key = str(path)
    with _registry_lock:
        mutex = _locks.setdefault(key, threading.RLock())
    with mutex:
        held = getattr(_held, "keys", set())
        if key in held:
            yield
            return
        fd = _open_lock(path)
        try:
            _os_lock(fd)
            _held.keys = held | {key}
            yield
        finally:
            _held.keys = held
            _unlock(fd)
            os.close(fd)


@contextmanager
def provider_slot(run_file, limit=3):
    """A global limit, not three new slots for each model worker."""
    directory = Path(run_file).resolve().parent
    acquired = None
    started = time.monotonic()
    while acquired is None:
        for index in range(limit):
            fd = _open_lock(directory / f".tyche-provider-{index}.guard")
            try:
                _os_lock(fd, blocking=False)
                acquired = fd
                break
            except (BlockingIOError, PermissionError):
                os.close(fd)
        if acquired is None:
            if time.monotonic() - started > 900:
                raise TimeoutError("Provider slots busy; no call dispatched")
            time.sleep(.05)
    try:
        yield
    finally:
        _unlock(acquired)
        os.close(acquired)


def state_path(run_file):
    return Path(run_file).with_name(Path(run_file).name + ".workers.json")


def snapshot(run_file):
    from budget_guard import read_object
    path = state_path(run_file)
    with locked(run_file):
        return read_object(path) if path.exists() else None


def update(run_file, change):
    from budget_guard import transaction
    with transaction(state_path(run_file)) as state:
        change(state)


def configure(run_file, count):
    def initialize(state):
        if state:
            if state.get("run_file") != str(Path(run_file).resolve()) or state.get("worker_count") != count:
                raise ValueError("Resume the original worker count and run; do not reset company claims")
            return
        state.update(version=1, run_file=str(Path(run_file).resolve()), worker_count=count,
                     phase="research", ready=False, workers={}, claims={}, aliases={}, conflicts=0)
    update(run_file, initialize)


def register(run_file, worker, generation):
    def assign(state):
        state["workers"][worker] = {"generation": generation, "status": "running"}
    update(run_file, assign)


def check_worker(state, worker, generation):
    current = state.get("workers", {}).get(worker, {})
    if (state.get("phase") != "research" or current.get("generation") != generation
            or current.get("status") != "running"):
        raise ValueError("This worker no longer owns an active research invocation; no research dispatched")


def company_key(value):
    value = str(value).strip()
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if parsed.username or parsed.password or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError("Claim a company by its website domain or verified LinkedIn company URL, not its name")
    if host == "linkedin.com":
        match = re.fullmatch(r"/company/([^/]+)/?", parsed.path)
        if not match:
            raise ValueError("Use a LinkedIn company URL, not a person profile")
        return "linkedin.com/company/" + match[1].lower()
    return host


def claim(run_file, worker, generation, target, aliases=(), *, allow_owned_complete=False):
    keys = list(dict.fromkeys(company_key(value) for value in (target, *aliases) if value))
    result = {}
    def reserve(state):
        check_worker(state, worker, generation)
        matches = {state["aliases"][key] for key in keys if key in state["aliases"]}
        if not matches and keys[0].startswith("linkedin.com/"):
            result.update(claimed=False, status="domain_required", target=keys[0],
                          next="Find the company's website domain in discovery, then claim that domain with company_url as its LinkedIn alias.")
            return
        for canonical in matches:
            row = state["claims"][canonical]
            if row["worker"] != worker or not allow_owned_complete and row.get("status") in {"accepted", "rejected"}:
                state["conflicts"] += 1
                result.update(claimed=False, target=canonical, owner=row["worker"], status=row["status"])
                return
        canonical = sorted(matches)[0] if matches else keys[0]
        row = state["claims"].setdefault(canonical, {"worker": worker, "status": "active",
            "claimed_at": datetime.now(timezone.utc).isoformat(), "aliases": []})
        for old in matches - {canonical}:
            row["aliases"].extend(state["claims"].pop(old)["aliases"])
        row["aliases"] = sorted(set(row["aliases"] + keys))
        for key in row["aliases"]:
            state["aliases"][key] = canonical
        result.update(claimed=True, target=canonical, owner=worker, status=row["status"])
    update(run_file, reserve)
    return result


def require_claim(run_file, worker, generation, target, aliases=()):
    state = snapshot(run_file)
    if state is None:
        return target
    check_worker(state, worker, generation)
    key = company_key(target)
    canonical = state["aliases"].get(key)
    row = state["claims"].get(canonical, {})
    if row.get("worker") != worker:
        raise ValueError("Claim this company with tyche_claim before research; it is unclaimed or owned by another worker")
    if aliases:
        result = claim(run_file, worker, generation, target, aliases, allow_owned_complete=True)
        if not result["claimed"]:
            raise ValueError("Company alias already belongs to " + result["owner"] + "; skip duplicate company research")
        canonical = result["target"]
    return canonical


def reviewed(run_file, worker, generation, target, decision):
    def mark(state):
        check_worker(state, worker, generation)
        canonical = state["aliases"].get(company_key(target))
        if canonical:
            if state["claims"][canonical]["worker"] != worker:
                raise ValueError("Another worker owns this company")
            state["claims"][canonical]["status"] = {"accept": "accepted", "reject": "rejected"}.get(decision, "active")
    update(run_file, mark)
