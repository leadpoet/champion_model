"""Continuously saved, reviewed leads; unfinished research stays in results.json."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import budget_guard
from record_route import mutate
from validate_run import _company_key, accepted_errors, qualification_errors


SCHEMA = "tyche.confirmed-leads.v1"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def output_path(run_file):
    path = Path(run_file)
    return path.with_name("leads.json" if path.name == "results.json" else path.stem + ".leads.json")


def _identity(run_file, document):
    return {"schema_version": SCHEMA, "run_id": document.get("run_id", document["request"].get("run_id")),
            "run_fingerprint": budget_guard.run_fingerprint(run_file),
            "request_fingerprint": fingerprint(document["request"]),
            "target_count": document["request"]["target_count"]}


def read(run_file, document):
    path = output_path(run_file)
    if path.is_symlink():
        raise ValueError("Confirmed leads must be a regular file, not a symlink")
    identity = _identity(run_file, document)
    if not path.exists():
        return {**identity, "confirmed_count": 0, "leads": []}
    saved = budget_guard.read_object(path)
    rows = saved.get("leads")
    if (any(saved.get(key) != value for key, value in identity.items())
            or not isinstance(rows, list) or saved.get("confirmed_count") != len(rows)
            or any(not isinstance(row, dict) or not _company_key(row) for row in rows)
            or len({_company_key(row) for row in rows}) != len(rows)):
        raise ValueError("Confirmed leads file is invalid or belongs to another run/request; original file preserved")
    return saved


def pending(run_file, document, scopes=None):
    saved = {_company_key(row): row for row in read(run_file, document)["leads"]}
    return [row for row in document["accepted"]
            if (scopes is None or _company_key(row) in scopes) and saved.get(_company_key(row)) != row]


def review_ref(run_file, document, scopes=None):
    return "confirmed:" + fingerprint({"request": document["request"],
                                       "accepted": pending(run_file, document, scopes)})


def preflight(run_file, document):
    # Check the whole accepted set so uniqueness/owner checks span earlier saves.
    scoped = dict(document, unresolved=[], rejected=[])
    errors = (qualification_errors(scoped, run_file=run_file)
              + accepted_errors(scoped, run_file=run_file))
    rows = scoped["accepted"]
    if len({_company_key(row) for row in rows}) != len(rows):
        errors.append("Confirmed leads require one row per company")
    return errors


def status(run_file, document, scopes=None):
    saved = read(run_file, document)
    current = {_company_key(row): row for row in document["accepted"]}
    retained = [row for row in saved["leads"] if current.get(_company_key(row)) == row]
    waiting = pending(run_file, document, scopes)
    return {"path": str(output_path(run_file)), "confirmed_count": len(retained),
            "pending_review": [_company_key(row) for row in waiting],
            "sync_required": len(retained) != len(saved["leads"]),
            "next": "Review the returned evidence with tyche_review before another lookup." if waiting else None}


def write_snapshot(path, document):
    """Readers see the old complete snapshot or the new complete snapshot."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".leads-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update(run_file, *, approval=None, scopes=None):
    """Retain unchanged confirmed rows; add new rows only with exact review approval.

    The existing results lock prevents reviews in another process from racing
    publication. The JSON itself stores the approved snapshot, so saving a lead
    requires no second approval file or final export to commit it.
    """
    result = {}

    def publish(document):
        saved = read(run_file, document)
        previous = {_company_key(row): row for row in saved["leads"]}
        rows = [row for row in document["accepted"] if previous.get(_company_key(row)) == row]
        waiting = pending(run_file, document, scopes)
        if approval is not None and waiting:
            if approval != review_ref(run_file, document, scopes):
                raise ValueError("Confirmed lead review changed; review the current packet")
            ledger = budget_guard.load_ledger(run_file)
            errors = budget_guard.audit_ledger(run_file, document, state=ledger, allow_pending=True)
            # Match save_review: a spending pause must not discard completed
            # research. Keep consistency checks and the unchanged ledger block.
            errors = [error for error in errors if error != ledger.get("blocked")]
            errors += preflight(run_file, document)
            if errors:
                raise ValueError("; ".join(errors))
            approved = {_company_key(row) for row in waiting}
            rows = [row for row in document["accepted"]
                    if _company_key(row) in approved or previous.get(_company_key(row)) == row]
        # An unrelated unfinished candidate or later provider error does not
        # invalidate an unchanged, already reviewed lead.
        if not output_path(run_file).exists() or rows != saved["leads"]:
            saved = {**_identity(run_file, document), "confirmed_count": len(rows),
                     "updated_at": datetime.now(timezone.utc).isoformat(), "leads": rows}
            write_snapshot(output_path(run_file), saved)
        result.update(path=str(output_path(run_file)), confirmed_count=len(rows), saved=True)
        return document

    mutate(run_file, publish)
    return result
