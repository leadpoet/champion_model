"""Map reviewed TYCHE records to Arena's current intent-details/contact schema."""

import hashlib
import json
import ipaddress
import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import budget_guard
import confirmed_leads
import linkedin_receipts
import run_attempt
from validate_run import _identity, accepted_errors, qualification_errors
from .constraints import check_contact
from .input import required_company_stage


CHECKPOINT_TRANSITION_REASONS = {
    "unchanged", "rejected", "unresolved", "changed_accepted",
    "missing_accepted", "mixed",
}


def canonical_output_sha256(rows):
    """Hash the canonical ASCII JSON envelope without retaining its payload."""
    encoded = json.dumps(
        {"companies": rows}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _row_identity(row):
    if not isinstance(row, dict):
        return None
    value = row.get("company_linkedin")
    if isinstance(value, str) and value:
        return value
    company = row.get("company", row.get("candidate"))
    if isinstance(company, dict):
        value = company.get("linkedin_url")
        if isinstance(value, str) and value:
            return value
    return None


def checkpoint_transition(run_file, checkpoint_rows, final_rows):
    """Describe one successful host checkpoint change with counts and hashes only."""
    final_hash = canonical_output_sha256(final_rows)

    def unchanged():
        return {
            "reason": "unchanged", "checkpoint_count": len(final_rows),
            "final_count": len(final_rows), "rejected_count": 0,
            "unresolved_count": 0, "changed_count": 0, "missing_count": 0,
            "checkpoint_sha256": final_hash, "final_sha256": final_hash,
        }

    if checkpoint_rows is None:
        return unchanged()
    try:
        if (not isinstance(checkpoint_rows, list) or not isinstance(final_rows, list)
                or not 0 <= len(checkpoint_rows) <= 5 or not 0 <= len(final_rows) <= 5):
            return unchanged()
        checkpoint_by_identity = {_row_identity(row): row for row in checkpoint_rows}
        final_by_identity = {_row_identity(row): row for row in final_rows}
        if (None in checkpoint_by_identity or None in final_by_identity
                or len(checkpoint_by_identity) != len(checkpoint_rows)
                or len(final_by_identity) != len(final_rows)):
            return unchanged()
        current = budget_guard.read_object(run_file)
        current_accepted_identities = {
            identity for identity in (
                _row_identity(row) for row in current.get("accepted", []))
            if identity is not None
        }
        current_by_identity = {}
        try:
            icp = json.loads(current["request"]["original_text"])
            projected = _project_companies(
                run_file, current, icp, require_review=False)
            current_by_identity = {
                _row_identity(row): row for row in projected
                if _row_identity(row) is not None
            }
        except (IndexError, KeyError, OSError, TypeError, ValueError):
            pass
        states = {}
        for state in ("rejected", "unresolved"):
            for row in current.get(state, []):
                identity = _row_identity(row)
                if identity:
                    states.setdefault(identity, set()).add(state)
        counts = {"rejected_count": 0, "unresolved_count": 0,
                  "changed_count": 0, "missing_count": 0}
        for identity, row in checkpoint_by_identity.items():
            if identity in final_by_identity:
                if final_by_identity[identity] != row:
                    counts["changed_count"] += 1
            elif (identity in current_by_identity
                  and current_by_identity[identity] != row):
                counts["changed_count"] += 1
            elif identity in current_accepted_identities:
                counts["missing_count"] += 1
            elif "rejected" in states.get(identity, ()):
                counts["rejected_count"] += 1
            elif "unresolved" in states.get(identity, ()):
                counts["unresolved_count"] += 1
            else:
                counts["missing_count"] += 1
        removed = sum(counts.values())
        checkpoint_hash = canonical_output_sha256(checkpoint_rows)
        active = [reason for reason, name in (
            ("rejected", "rejected_count"), ("unresolved", "unresolved_count"),
            ("changed_accepted", "changed_count"), ("missing_accepted", "missing_count"),
        ) if counts[name]]
        if (len(checkpoint_rows) != len(final_rows) + removed
                or (checkpoint_hash == final_hash) != (not active)):
            return unchanged()
        return {
            "reason": "unchanged" if not active else active[0] if len(active) == 1 else "mixed",
            "checkpoint_count": len(checkpoint_rows), "final_count": len(final_rows),
            **counts, "checkpoint_sha256": checkpoint_hash, "final_sha256": final_hash,
        }
    except (OSError, TypeError, ValueError):
        return unchanged()


def projected_payload(rows, targets=()):
    """Match Arena public output limits before approval or a checkpoint write."""
    document = {"companies": rows}
    fields = {"companies", "company_name", "company_website", "company_linkedin", "industry",
              "employee_count", "company_stage", "country", "state", "intent_details",
              "intent_signals", "required_attribute", "contact", "matched_icp_signal",
              "description", "date", "url", "text", "passed", "evidence_url", "evidence_quote",
              "explanation", "full_name", "role", "linkedin_url", "email", "location", "region",
              "city", "email_source", "provider", "tool", "record_id"}

    def fail(path, reason):
        match = re.search(r"^\$\.companies\[(\d+)\]", path)
        index = int(match[1]) if match else None
        target = (" candidate " + str(index + 1) + " target "
                  + json.dumps(targets[index], ensure_ascii=True)
                  if index is not None and index < len(targets) else " projected document")
        guidance = (" Select a shorter exact supported quote from the same saved source ref with tyche_review; no lookup replay is needed."
                    if path.endswith(".evidence_quote") else " Repair this field with tyche_review before approval.")
        raise ValueError("Arena output" + target + " at " + path + ": " + reason + guidance)

    def check_string(value, path):
        size = len(value.encode("utf-8", errors="surrogatepass"))
        if size > 4096:
            fail(path, "UTF-8 string length " + str(size) + " bytes exceeds 4096 bytes")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            fail(path, "forbidden control character in " + str(size) + " UTF-8 bytes")
        if re.search(r"[\ud800-\udfff]", value):
            fail(path, "unpaired surrogate in " + str(size) + " UTF-8 bytes")

    def check(value, depth, path):
        if depth > 8:
            fail(path, "nesting depth " + str(depth) + " exceeds 8")
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int):
            if abs(value) > 2 ** 53:
                fail(path, "integer magnitude " + str(abs(value)) + " exceeds 2**53")
        elif isinstance(value, float):
            if not math.isfinite(value):
                fail(path, "non-finite number")
        elif isinstance(value, str):
            check_string(value, path)
        elif isinstance(value, (list, tuple)):
            if len(value) > 200:
                fail(path, "list length " + str(len(value)) + " exceeds 200")
            for index, item in enumerate(value):
                check(item, depth + 1, path + "[" + str(index) + "]")
        elif isinstance(value, Mapping):
            if len(value) > 64:
                fail(path, "object key count " + str(len(value)) + " exceeds 64")
            for index, (key, item) in enumerate(value.items()):
                child = path + ("." + key if isinstance(key, str) and key in fields else ".[key" + str(index) + "]")
                if not isinstance(key, str):
                    fail(child, "object key must be text")
                check_string(key, child)
                check(item, depth + 1, child)
        else:
            fail(path, "unsupported JSON value type")

    check(document, 0, "$")
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        payload = (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    except (TypeError, ValueError):
        fail("$", "unsupported JSON representation")
    for name, encoded in (("canonical document", canonical), ("encoded output", payload)):
        if len(encoded) > 524288:
            fail("$", name + " length " + str(len(encoded)) + " bytes exceeds 524288 bytes")
    return payload


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value.strip()


def evidence_value(evidence, key):
    return evidence.get("evidence_" + key, evidence.get(key))


def signal_date(evidence):
    """Project reviewed activity timing into Arena V5 without inventing precision."""
    value = evidence.get("event_date")
    if value is None and evidence_value(evidence, "date_basis") == "observed_current":
        value = evidence_value(evidence, "date")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def public_url(value):
    parsed = urlsplit(text(value, "URL"))
    host = (parsed.hostname or "").rstrip(".").lower()
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
            or host == "localhost" or host.endswith((".internal", ".invalid", ".local", ".localhost", ".onion", ".test"))):
        raise ValueError("Arena requires a public HTTP URL")
    parsed.port  # Reject malformed ports too.
    try:
        host.encode("idna")
    except UnicodeError:
        raise ValueError("Arena requires an IDNA-encodable public HTTP hostname") from None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not any(c.isalpha() for c in host.rsplit(".", 1)[1]):
            raise ValueError("Arena requires a public HTTP URL")
    else:
        if not address.is_global:
            raise ValueError("Arena requires a public HTTP URL")
    return value


def companies(run_file, icp):
    document = budget_guard.read_object(run_file)
    rows = reviewed_companies(run_file, document, icp)
    _, validation = run_attempt.delivery_preflight(run_file, document)
    if not validation["delivery_allowed"]:
        raise ValueError("; ".join(validation["errors"]))
    return rows


def accepted_preflight(run_file, document):
    """Validate completed leads without declaring the unfinished run complete."""
    completed = dict(document, rejected=[], unresolved=[])
    return (qualification_errors(completed, run_file=run_file)
            + accepted_errors(completed, run_file=run_file))


def _project_companies(run_file, document, icp, *, require_review):
    if json.loads(document["request"]["original_text"]) != icp:
        raise ValueError("Arena delivery ICP differs from the saved request")
    final_approved = (document.get("final_review", {}).get("review_ref")
                      == run_attempt.review_fingerprint(document))
    incremental_approved = (document.get("confirmed_review", {}).get("review_ref")
                            == confirmed_fingerprint(document))
    if require_review and not final_approved and not incremental_approved:
        raise ValueError("Approve the current evidence review before Arena delivery")
    if errors := accepted_preflight(run_file, document):
        raise ValueError("; ".join(errors))
    output = []
    kinds = {_identity(signal["kind"]): index for index, signal in enumerate(document["request"]["buying_signals"])}
    for row in document["accepted"]:
        company, person = row["company"], row["primary_contact"]
        stage = company.get("company_stage")
        if stage is None:
            stage = ""
        elif not isinstance(stage, str):
            raise ValueError("Arena company_stage must be text when supplied")
        if required_company_stage(icp) and not stage.strip():
            raise ValueError("Set company.company_stage with tyche_review to the observed current stage label supported by its reviewed evidence")
        check_contact(person, icp)
        signals = []
        attribute = None
        for check in row["qualification_checks"]:
            if check["status"] != "pass":
                continue
            proof = next((e for e in check.get("evidence", []) if evidence_value(e, "url")), None)
            if not proof:
                continue
            if _identity(check.get("signal")) in kinds:
                signals.append({"matched_icp_signal": kinds[_identity(check["signal"])], "description": check["claim"],
                    "date": signal_date(proof), "url": evidence_value(proof, "url")})
            if icp.get("required_attribute") and not check.get("signal") and _identity(check["criterion"]) == _identity(icp["required_attribute"]):
                attribute = {"text": icp["required_attribute"], "passed": True,
                    "evidence_url": evidence_value(proof, "url"), "evidence_quote": evidence_value(proof, "text"),
                    "explanation": check["claim"]}
        if not any(signal["matched_icp_signal"] == 0 for signal in signals) or icp.get("required_attribute") and attribute is None:
            raise ValueError("Arena requires mapped signal and required-attribute evidence")
        for signal in signals:
            public_url(signal["url"])
        if attribute:
            public_url(attribute["evidence_url"])
        paragraph = text(row.get("intent_details"), "intent_details")
        if (len(paragraph) > 2000 or re.search(r"\n\s*\n|(?:^|\n)\s*(?:#{1,6}\s|[-*•]\s|\d+[.)]\s|>)", paragraph)
                or "```" in paragraph or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\r\n\t" for c in paragraph)):
            raise ValueError("Arena intent_details requires one plain paragraph of at most 2000 characters")
        source = (person.get("location_evidence") or person)["source"]
        profile = linkedin_receipts._saved_profile(run_file, source, person["linkedin_url"], "in",
            document["routes"], company.get("linkedin_url"))
        receipt = run_attempt.read_receipt(run_file, source["route_id"])["result"]
        if receipt["attempt"]["request"].get("payload", {}).get("findEmail") != "true":
            raise ValueError("Arena email must come from HarvestAPI get_profile with findEmail=true")
        # Use only the selected raw profile, never an LLM-authored email_source.
        emails = profile.get("emails", [])
        observed = {str(e.get("email") if isinstance(e, dict) else e).strip().casefold() for e in emails}
        if profile.get("email"):
            observed.add(str(profile["email"]).strip().casefold())
        if person["email"].strip().casefold() not in observed:
            raise ValueError("Arena email is absent from the selected provider profile")
        record_id = text(profile.get("recordId") or profile.get("record_id") or profile.get("id"), "HarvestAPI record ID")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/~-]{0,199}", record_id):
            raise ValueError("HarvestAPI record ID violates Arena's source contract")
        contact = {"full_name": person["full_name"], "role": person["current_title"],
            "linkedin_url": person["linkedin_url"], "email": person["email"],
            "location": {"country": person["country"], **({"region": person["state"]} if person.get("state") else {}),
                         **({"city": person["city"]} if person.get("city") else {})},
            "email_source": {"provider": "harvestapi", "tool": "harvestapi_get_profile", "record_id": record_id}}
        output.append({"company_name": company["canonical_name"],
            "company_website": public_url(company.get("website") or "https://" + company["domain"]),
            "company_linkedin": company["linkedin_url"], "industry": company["industry"],
            "employee_count": company["employee_range"], "company_stage": stage,
            "country": text(company.get("hq_country"), "company country"), "state": company.get("hq_state", ""),
            "intent_details": " ".join(paragraph.split()), "intent_signals": signals,
            "required_attribute": attribute, "contact": contact})
    if len(output) > min(5, document["request"]["target_count"]):
        raise ValueError("Arena company limit exceeded")
    projected_payload(output, [row["company"]["domain"] for row in document["accepted"]])
    return output


def projection_preflight(run_file, document, icp):
    """Return Arena projection errors before approving the native final review."""
    try:
        _project_companies(run_file, document, icp, require_review=False)
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        return ["Arena output projection: " + str(exc)]
    return []


def reviewed_companies(run_file, document, icp):
    return _project_companies(run_file, document, icp, require_review=True)


def deliver(run_file, validation, icp, checkpoint=None, *, partial=False):
    document = budget_guard.read_object(run_file)
    rows = reviewed_companies(run_file, document, icp) if partial else companies(run_file, icp)
    return publish(run_file, document, rows, validation, checkpoint, partial=partial)


def confirmed_fingerprint(document):
    return confirmed_leads.fingerprint({"request": document["request"], "accepted": document["accepted"]})


def confirmed_document(document, rows):
    """Project an already approved subset without approving new research."""
    document = dict(document, accepted=rows, unresolved=[], rejected=[])
    document.pop("final_review", None)
    document["confirmed_review"] = {"review_ref": confirmed_fingerprint(document)}
    return document


def publish_confirmed(run_file, icp, checkpoint, output_path):
    """Publish the native approved snapshot before returning review or doing more work."""
    confirmed_leads.update(run_file)
    document = budget_guard.read_object(run_file)
    confirmed = confirmed_leads.read(run_file, document)
    snapshot = Path(run_file).with_name("checkpoint-results.json")
    if not confirmed["leads"] and not snapshot.exists() and not Path(output_path).exists():
        return None  # A draft is not an empty successful checkpoint.
    document = confirmed_document(document, confirmed["leads"])
    document["confirmed_review"]["reviewed_at"] = confirmed["updated_at"]
    rows = reviewed_companies(run_file, document, icp)
    if snapshot.exists():
        previous = budget_guard.read_object(snapshot)
        if previous.get("confirmed_review") == document["confirmed_review"]:
            # An acknowledgement may have been lost. Verify, never redispatch research.
            checkpointed_companies(run_file, icp, output_path)
            return {"checkpoint_saved": True, "confirmed_count": len(rows), "output": str(output_path)}
    result = publish(run_file, document, rows, {"valid": True, "scope": "confirmed_leads"},
                     checkpoint, partial=True)
    return {"checkpoint_saved": result["checkpoint_saved"], "confirmed_count": len(rows),
            "output": str(output_path)}


def publish(run_file, document, rows, validation, checkpoint, *, partial):
    result = {"companies": rows}
    path = Path(run_file).with_name("companies.json")
    projected_payload(rows, [row["company"]["domain"] for row in document["accepted"]])
    if checkpoint:
        checkpoint(rows)
    confirmed_leads.write_snapshot(path, result)
    confirmed_leads.write_snapshot(Path(run_file).with_name("validation.json"), validation)
    # Preserve the reviewed state independently of candidates still in progress.
    # A failed host write never advances this committed snapshot.
    snapshot = path.with_name("checkpoint-results.json")
    confirmed_leads.write_snapshot(snapshot, document)
    return {"delivery_allowed": not partial, "checkpoint_saved": True,
            "companies": rows, "output": str(path)}


def read_output(path):
    """Read one bounded host checkpoint with the exact Arena envelope."""
    with Path(path).open("rb") as stream:
        payload = stream.read(512 * 1024 + 1)
    if len(payload) > 512 * 1024:
        raise ValueError("Arena output exceeds 512 KiB")
    output = json.loads(payload)
    if (not isinstance(output, dict) or set(output) != {"companies"}
            or not isinstance(output["companies"], list)
            or any(not isinstance(row, dict) for row in output["companies"])):
        raise ValueError("Arena output must contain a companies list")
    return output


def _same_run(document, current):
    """Reject a local snapshot copied from another run or request."""
    request = document.get("request")
    current_request = current.get("request")
    run_id = document.get("run_id", request.get("run_id") if isinstance(request, dict) else None)
    current_id = current.get(
        "run_id", current_request.get("run_id") if isinstance(current_request, dict) else None)
    return (isinstance(run_id, str) and run_id
            and run_id == current_id and request == current_request)


def checkpoint_documents(run_file, current, approved):
    yield confirmed_document(current, approved)
    snapshot = Path(run_file).with_name("checkpoint-results.json")
    if snapshot.exists():
        document = budget_guard.read_object(snapshot)
        if _same_run(document, current):
            yield document


def checkpointed_companies(run_file, icp, output_path, *, checkpoint=None):
    """Recover the host commit, then revoke rows no longer confirmed in this run."""
    try:
        output = read_output(output_path)
    except FileNotFoundError as exc:
        raise ValueError("No reviewed TYCHE checkpoint was delivered") from exc
    current = budget_guard.read_object(run_file)
    confirmed = confirmed_leads.read(run_file, current)["leads"]
    # A newer approval may not have reached the host. Select only host-listed
    # identities, then require an exact, fully validated projection below.
    identities = {row.get("company_linkedin") for row in output["companies"]
                  if isinstance(row.get("company_linkedin"), str)}
    approved = ([row for row in confirmed if row["company"]["linkedin_url"] in identities]
                if identities else confirmed)
    for document in checkpoint_documents(run_file, current, approved):
        try:
            rows = reviewed_companies(run_file, document, icp)
        except (KeyError, TypeError, ValueError):
            continue
        if output != {"companies": rows}:
            continue
        retained = [row for row in document["accepted"]
                    if row in confirmed and row in current["accepted"]]
        if retained != document["accepted"]:
            rows = reviewed_companies(
                run_file, confirmed_document(document, retained), icp)
            if checkpoint is None:
                raise ValueError(
                    "Checkpoint includes changed or withdrawn leads; publish their removal before delivery")
            checkpoint(rows)
            if read_output(output_path) != {"companies": rows}:
                raise ValueError("Lab output differs from the reviewed TYCHE checkpoint")
        return rows
    raise ValueError("Lab output differs from the reviewed TYCHE checkpoint")
