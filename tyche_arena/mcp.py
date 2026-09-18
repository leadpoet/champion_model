"""Run-bound native TYCHE tools inside the lab's existing gVisor sandbox."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import threading

from .broker import Broker
from .output import deliver, projection_preflight, publish_confirmed
from .public_web import PublicWeb
import confirmed_leads
from email_receipts import verification_status_parent
from research_tools import ResearchTools, TOOLS, validate
import budget_guard
from tyche_tools import serve


def arena_schema(schema):
    """Hoist nested shapes to fit PR #198's 12-level operation JSON ceiling."""
    definitions = {}

    def visit(value, root=False):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(child) for key, child in value.items()}
        if not root and value.get("type") in {"object", "array"}:
            key = "Shape" + str(len(definitions))
            definitions[key] = result
            return {"$ref": "#/$defs/" + key}
        return result

    result = visit(schema, root=True)
    if definitions:
        result["$defs"] = definitions
    return result


def lab_tools():
    tools = copy.deepcopy(TOOLS)
    del tools["tyche_start"]
    del tools["tyche_review"][1]["properties"]["web"]
    review_description, review_schema = tools["tyche_review"]
    review_description = review_description.replace(
        "For web qualifications use a page captured by tyche_lookup (ScrapingDog scrape or a Deepline page reader); web observations are discovery notes, not qualifying evidence.",
        "For web qualifications use a successful research page captured by tyche_open or tyche_lookup (ScrapingDog scrape or a Deepline page reader), under the same native quote and date checks; authored web notes and finalization rereads remain nonqualifying observations.",
    )
    review_description += (
        " Arena validates the accepted lead projection before approval and publishes the native "
        "confirmed snapshot through the host checkpoint writer after approval."
    )
    review_schema["properties"]["review_ref"] = {
        "type": "string", "minLength": 1,
        "description": "Approve the exact evidence packet returned after an accepted-company review and save it to Arena.",
    }
    review_schema["properties"]["companies"]["items"]["properties"]["company"]["properties"]["company_stage"] = {
        "type": "string",
        "description": "For an Arena stage constraint, supply the concise observed current stage label supported by the same reviewed stage evidence (for example Series B); omit explanatory prose and never copy the requested stage without proof.",
    }
    company_fields = review_schema["properties"]["companies"]["items"]["properties"]
    for contact in (company_fields["primary_contact"], company_fields["backup_contacts"]["items"]):
        contact["properties"]["email"] = {
            "description": "The exact chosen address from saved discovery evidence; selecting it does not verify it. email_source attributes this same address.",
        }
        contact["properties"]["email_ref"]["description"] = (
            "A saved same-address ZeroBounce or eligible BounceBan validation verdict, separate from finder provenance in email_source."
        )
    tools["tyche_review"] = review_description, review_schema
    tools["tyche_open"] = (
        "Read one exact public HTTP(S) page through the Arena host proxy. Native TYCHE first "
        "validates and saves the free public-web plan. Repeated reads of the same target, URL "
        "and research/finalization phase reuse its immutable observation. These observations "
        "from successful research reads are tool-captured page evidence. Reuse their refs under "
        "the same native quote, date and qualification checks. Finalization rereads are "
        "corroboration only; legacy authored observations remain discovery notes.",
        {"type": "object", "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 253},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
            "url": {"type": "string", "minLength": 1, "maxLength": 4096},
        }, "required": ["target", "purpose", "url"], "additionalProperties": False})
    tools["tyche_checkpoint"] = (
        "Compatibility checkpoint tool. Normally tyche_review approval saves automatically. "
        "Review the evidence packet, then approve its current review_ref with company-specific "
        "review_findings. Only reviewed, fully "
        "qualified companies and contacts are checkpointed for the lab deadline. This does not "
        "end research or change the target; use tyche_finish to close the run.",
        {"type": "object", "properties": {
            key: copy.deepcopy(TOOLS["tyche_finish"][1]["properties"][key])
            for key in ("review_ref", "review_findings")
        },
         "additionalProperties": False})
    return {name: (description, arena_schema(schema)) for name, (description, schema) in tools.items()}


LAB_TOOLS = lab_tools()
MODEL_RESULT_MAX_CHARACTERS = 24000
EVIDENCE_REVIEW_PAGE_CHARACTERS = 8000


def broker_resume_state(run_file):
    """Restore local dispatch safety from this run's durable routes and receipts."""
    run_file = Path(run_file).resolve(strict=True)
    ledger = budget_guard.load_ledger(run_file)
    calls = ledger.get("calls") if isinstance(ledger, dict) else None
    if not isinstance(calls, dict) or any(
            not isinstance(route_id, str) or not isinstance(call, dict)
            or call.get("provider") not in budget_guard.PROVIDERS
            for route_id, call in calls.items()):
        raise ValueError("Arena run ledger has invalid provider calls")
    call_ids = {provider: {route_id for route_id, call in calls.items() if call["provider"] == provider}
                for provider in budget_guard.PROVIDERS}
    blocked = {provider: False for provider in budget_guard.PROVIDERS}
    try:
        document = budget_guard.read_object(run_file)
        routes = document.get("routes")
        if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
            raise ValueError("invalid saved routes")
        for provider in budget_guard.PROVIDERS:
            paid_routes = [route for route in routes
                           if route.get("provider") == provider and route.get("paid_calls") == 1]
            saved_routes = {route.get("route_id"): route for route in paid_routes}
            blocked[provider] = (len(saved_routes) != len(paid_routes)
                                 or set(saved_routes) != call_ids[provider])
            for route_id in call_ids[provider] & set(saved_routes):
                path = run_file.parent / "receipts" / (route_id + ".json")
                try:
                    receipt = budget_guard.read_object(path)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    blocked[provider] = True
                    continue
                if (receipt.get("receipt_status") != "complete"
                        or receipt.get("run_fingerprint") != budget_guard.run_fingerprint(run_file)
                        or receipt.get("provider") != provider
                        or receipt.get("request_fingerprint") != saved_routes[route_id].get("request_fingerprint")):
                    blocked[provider] = True
                    continue
                raw = receipt.get("provider_response")
                if not isinstance(raw, dict) or raw.get("timed_out") is True:
                    blocked[provider] = True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Preserve checkpoints and allow inspection/finalization, but never
        # resume paid research from malformed or incomplete durable state.
        blocked = {provider: True for provider in budget_guard.PROVIDERS}
    return ({provider: len(call_ids[provider]) for provider in budget_guard.PROVIDERS}, blocked)


def watch_parent(parent_pid, stopped):
    """Codex launches MCP in its own process group; follow its lifetime too."""
    while not stopped.wait(0.25):
        if parent_pid <= 1 or os.getppid() != parent_pid:
            # An interrupted call retains its saved reservation. Never let an
            # orphan keep spending or publish a late checkpoint after Codex dies.
            os._exit(1)


def model_result(result, budget=None):
    if budget is not None:
        result = {**result, "arena_budget": budget}
    encoded = json.dumps(result, ensure_ascii=True)
    if len(encoded) <= MODEL_RESULT_MAX_CHARACTERS:
        return result
    return {"truncated": True, "status": result.get("status"), "review_ref": result.get("review_ref"),
            "review_scope": result.get("review_scope"), "confirmed_leads": result.get("confirmed_leads"),
            "arena_checkpoint": result.get("arena_checkpoint"),
            "arena_budget": result.get("arena_budget"),
            "preview": encoded[:8000],
            "next": "Read narrower fields with tyche_inspect. Inspect each listed company's evidence_review before returning review_ref to the requesting tool. This preview is incomplete."}


def public_web_model_result(result, budget):
    """Keep the durable ref when an escaped 8K page preview exceeds MCP output."""
    wrapped = model_result(result, budget)
    if (wrapped.get("truncated") is not True or "preview" not in wrapped
            or "ref" in wrapped or not isinstance(result.get("text"), str)):
        return wrapped
    ref = result.get("ref")
    compact = {
        "status": result.get("status"), "ref": ref, "cached": result.get("cached"),
        "content_sha256": result.get("content_sha256"),
        "saved_characters": result.get("saved_characters"),
        "observed_characters": result.get("observed_characters"),
        "source_truncated": result.get("truncated", False),
        "preview_omitted": True, "next_offset": 0,
        "next": {"tool": "tyche_inspect", "arguments": {
            "ref": ref, "field": "text", "offset": 0}},
    }
    return model_result(compact, budget)


def _lookup_failure_guidance(recorded):
    if recorded:
        return "Inspect the saved route for the complete outcome before retrying."
    return (
        "No saved route result is available for this outcome. recorded=false does not prove "
        "request_sent=false. Correct a confirmed pre-dispatch refusal or choose a supported alternative. "
        "For uncertain work, use tyche_inspect(recover=route-id) only when a saved receipt exists; "
        "never retry an uncertain paid call."
    )


def _bounded_error_text(error, limit=1200):
    if not isinstance(error, str) or len(json.dumps(error, ensure_ascii=True)) <= limit:
        return error
    suffix = "… [error abridged]"
    low, high = 0, len(error)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(error[:middle] + suffix, ensure_ascii=True)) <= limit:
            low = middle
        else:
            high = middle - 1
    return error[:low] + suffix


def _bounded_progress_errors(errors, limit=4000):
    """Keep stop diagnostics visible without allowing them to consume the MCP bound."""
    if not isinstance(errors, list):
        return errors
    retained = []
    for error in errors:
        error = _bounded_error_text(error)
        candidate = retained + [error]
        if len(json.dumps(candidate, ensure_ascii=True)) > limit:
            break
        retained.append(error)
    if len(retained) < len(errors):
        while True:
            marker = f"… [{len(errors) - len(retained)} more stop errors omitted]"
            if len(json.dumps(retained + [marker], ensure_ascii=True)) <= limit or not retained:
                break
            retained.pop()
        retained.append(marker)
    return retained


def lookup_model_result(result, budget):
    """Keep saved lookup references when facts exceed the model response bound."""
    wrapped = model_result(result, budget)
    if wrapped.get("truncated") is not True:
        return wrapped
    views = result.get("lookups")
    single_view = not isinstance(views, list)
    if single_view:
        if not result.get("route") or not isinstance(result.get("results"), list):
            return wrapped
        views = [result]
    lookups = []
    for lookup in views:
        summary = {key: lookup[key] for key in (
            "route", "status", "recorded", "request_sent", "result_count", "next_offset", "pending_verification"
        ) if key in lookup}
        summary["results"] = []
        for row in lookup.get("results", []):
            item = {"ref": row["ref"]}
            if single_view and isinstance(row.get("facts"), dict):
                fields = list(row["facts"])[:20]
                while len(json.dumps(fields, ensure_ascii=True)) > 1200:
                    fields.pop()
                item["available_fields"] = fields
                item["omitted_field_count"] = len(row["facts"]) - len(fields)
            summary["results"].append(item)
        for key in ("error", "recovery_note", "selection_note", "email_search_guidance", "catalog_note"):
            if key in lookup:
                value = lookup[key]
                encoded = json.dumps(value, ensure_ascii=True)
                summary[key] = value if len(encoded) <= 1200 else {
                    **({field: value[field] for field in (
                        "code", "kind", "type", "status", "retryable", "request_sent"
                    ) if field in value and len(json.dumps(value[field], ensure_ascii=True)) <= 200}
                       if isinstance(value, dict) else {}),
                    "preview": encoded[:1000], "preview_omitted": True,
                    "next": _lookup_failure_guidance(lookup.get("recorded") is True),
                }
        summary["preview_omitted"] = True
        if lookup.get("recorded") is True:
            summary["next"] = {"tool": "tyche_inspect", "arguments": {
                "ref": lookup["route"], "offset": 0, "limit": 1,
            }}
        else:
            summary["next"] = _lookup_failure_guidance(False)
        lookups.append(summary)
    if single_view:
        single_next = (
            "This saved page is too large. Use tyche_inspect with a listed result ref and one available field. "
            "For text, follow next_offset to read every page; the route next_offset still pages results. "
            "No lookup needs repeating."
            if lookups[0].get("recorded") is True else _lookup_failure_guidance(False)
        )
        return model_result({**lookups[0], "next": single_next}, budget)
    compact = {key: result[key] for key in (
        "status", "delivery_allowed", "reason", "resume", "costs", "summary",
        "confirmed_leads", "arena_checkpoint", "checkpoint_saved"
    ) if key in result}
    if isinstance(result.get("progress"), dict):
        compact["progress"] = {key: result["progress"][key] for key in (
            "summary", "company_count", "confirmed_leads", "elapsed_seconds", "budget", "stop", "errors"
        ) if key in result["progress"]}
        if "errors" in compact["progress"]:
            compact["progress"]["errors"] = _bounded_progress_errors(compact["progress"]["errors"])
    all_recorded = all(lookup.get("recorded") is True for lookup in lookups)
    compact.update({
        "lookups": lookups, "preview_omitted": True,
        "next": (
            "Facts remain saved in full. Inspect each recorded route from offset 0, then follow its next_offset. "
            "Use a result ref and field to read narrower fields or page text. No lookup needs repeating."
            if all_recorded else
            "Inspect only recorded routes from offset 0, then follow their next_offset. Unrecorded outcomes have no "
            "saved route result to inspect; use tyche_inspect(recover=route-id) only when a saved receipt exists. "
            "recorded=false does not prove request_sent=false; never retry an uncertain paid call."
        ),
    })
    return model_result(compact, budget)


def evidence_review_page(result, offset):
    """Expose one stable page of a complete derived review without changing it."""
    content = json.dumps(
        result, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
        sort_keys=True,
    )
    next_offset = min(len(content), offset + EVIDENCE_REVIEW_PAGE_CHARACTERS)
    return {
        "status": "evidence_review_page",
        "content": content[offset:next_offset],
        "content_sha256": hashlib.sha256(content.encode("ascii")).hexdigest(),
        "total_characters": len(content),
        "offset": offset,
        "next_offset": next_offset if next_offset < len(content) else None,
        "encoding": "JSON with ensure_ascii=true, sorted keys, and compact separators",
        "next": (
            "Request each page with the returned next_offset. Require identical "
            "content_sha256 and total_characters on every page; if either changes, "
            "restart at offset 0. Concatenate content in offset order, then parse "
            "the reconstructed JSON. Read all pages before explicitly approving "
            "review_ref. Paging does not approve review_ref."
        ),
    }


class LabTools:
    def __init__(self, run_file, deadline, response_deadline=None):
        import lab_arena_checkpoint

        provider_calls, provider_blocked = broker_resume_state(run_file)
        self.broker = Broker(os.environ["LAB_ARENA_WORKER_SOCKET"], deadline,
                             response_deadline=response_deadline,
                             initial_calls=provider_calls,
                             provider_blocked=provider_blocked)
        icp = json.loads(json.loads(Path(run_file).read_text())["request"]["original_text"])
        self.lock = threading.Lock()
        self.delivered = False
        self.icp = icp
        self.write_checkpoint = lab_arena_checkpoint.write
        self.output_path = os.environ["LAB_ARENA_OUTPUT_PATH"]

        def save(path, validation):
            result = deliver(path, validation, icp, lab_arena_checkpoint.write)
            self.delivered = True
            return result

        self.research = ResearchTools(run_file, execute=self._execute, deliver=save)
        self.public_web = PublicWeb(self.research, response_deadline or deadline)
        self._native_review_delivery = self.research.review_delivery

    def _execute(self, request, capture):
        """Let only native-authorized free verification recovery use finalization time."""
        allow_after_deadline = False
        try:
            owner = capture.__self__
            metadata = owner.metadata
            action = metadata["attempt"]["action"]
            document = budget_guard.read_object(self.research.path)
            allow_after_deadline = bool(
                verification_status_parent(self.research.path, document, action, request)
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            # Only the exact validated native attempt can receive this narrow
            # exception. Direct, malformed or damaged-state adapter calls keep
            # the normal research deadline.
            pass
        return self.broker.execute(
            request, capture, allow_after_deadline=allow_after_deadline
        )

    def _accepted_source_url(self, url):
        """Allow only exact URLs already saved in the pending native review."""
        def urls(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"url", "evidence_url"} and isinstance(child, str):
                        yield child
                    yield from urls(child)
            elif isinstance(value, list):
                for child in value:
                    yield from urls(child)

        document = self.research._document()
        return url in set(urls(confirmed_leads.pending(self.research.path, document)))

    def _projection_repair(self, document):
        errors = projection_preflight(self.research.path, document, self.icp)
        if not errors:
            return None
        # Native update removes changed or withdrawn rows without approving the
        # invalid pending revision. Publish that retained subset, including an
        # empty list, so Arena never keeps a stale positive checkpoint.
        saved = self._publish_confirmed()
        result = {"status": "needs_repair", "delivery_allowed": False,
                "checkpoint_saved": False, "errors": errors,
                "confirmed_leads": confirmed_leads.status(self.research.path, document),
                "next": "Correct or hold the named lead with tyche_review. The invalid pending revision was not approved; the retained confirmed subset remains saved."}
        if saved:
            result["arena_checkpoint"] = saved
        return result

    def _publish_confirmed(self):
        return publish_confirmed(
            self.research.path, self.icp, self.write_checkpoint,
            self.output_path,
        )

    def _review_delivery(self, document, review_ref=None, review_findings=None):
        errors = projection_preflight(self.research.path, document, self.icp)
        if errors:
            return {"status": "needs_repair", "delivery_allowed": False, "errors": errors,
                    "next": "Correct the named Arena output fields with review/inspect before final evidence review. No approval or delivery occurred."}
        return self._native_review_delivery(document, review_ref, review_findings)

    def checkpoint(self, review_ref=None, review_findings=None):
        document = self.research._document()
        if repair := self._projection_repair(document):
            return repair
        with self.research._review_lock:
            result = self.research._confirm_leads(review_ref, review_findings)
        if result.get("status") != "confirmed_leads_saved":
            return result
        saved = self._publish_confirmed()
        if not saved:
            return result
        return {**result, "status": "checkpoint_saved", "checkpoint_saved": True,
                "arena_checkpoint": saved,
                "next": "Reviewed companies are saved. Continue research toward the original target, then tyche_finish."}

    def _inspect_lab_tool(self, arguments):
        """Use native field/paging semantics with the exact served Arena contract."""
        validate(arguments, LAB_TOOLS["tyche_inspect"][1])
        if any(arguments.get(key) is not None for key in ("target", "ref", "query", "recover")):
            raise ValueError("Inspect one company, result, capability query, tool or recovery reference at a time")
        name = arguments["tool"]
        description, schema = LAB_TOOLS[name]
        contract = {"toolId": name, "description": description, "inputSchema": schema}
        if not arguments.get("field"):
            return {"tool": self.research._description_view(contract)}
        value = self.research._field(contract, arguments["field"])
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        if isinstance(value, str):
            return {"tool": value[offset:offset + 1800], "total_characters": len(value),
                    "next_offset": offset + 1800 if offset + 1800 < len(value) else None}
        if isinstance(value, list):
            return {"tool": copy.deepcopy(value[offset:offset + limit]), "total": len(value),
                    "next_offset": offset + limit if offset + limit < len(value) else None}
        return {"tool": copy.deepcopy(value)}

    def call(self, name, arguments):
        if name not in LAB_TOOLS:
            raise ValueError("The lab initialized this run; use its bound research tools")
        if name == "tyche_review" and "web" in arguments:
            raise ValueError("Lab evidence must come through the bound provider adapter")
        # The MCP transport dispatches up to three calls concurrently, while
        # Arena's timeout covers one native lookup batch. Refuse overlap before
        # reading or changing run state instead of hiding queue time inside a
        # second tool request. The refused request is safe to retry after the
        # active response because it created no attempt or provider work.
        if not self.lock.acquire(blocking=False):
            return model_result({
                "status": "arena_busy",
                "request_sent": False,
                "retryable": True,
                "next": (
                    "Another Arena tool call is active. Wait for its result, then retry this exact "
                    "refused call once. No attempt, reservation, or provider request was created."
                ),
            }, self.broker.local_dispatch_budget())
        try:
            if self.delivered and (name != "tyche_inspect" or any(key in arguments for key in ("recover", "refresh", "query", "tool"))):
                raise ValueError("Reviewed JSON is delivered; end the Codex turn now")
            if name == "tyche_inspect" and arguments.get("tool") in LAB_TOOLS:
                result = self._inspect_lab_tool(arguments)
            elif name == "tyche_checkpoint":
                validate(arguments, LAB_TOOLS[name][1])
                result = self.checkpoint(**arguments)
            elif name == "tyche_review":
                validate(arguments, LAB_TOOLS[name][1])
                document = self.research._document()
                if arguments.get("review_ref") is not None and (repair := self._projection_repair(document)):
                    result = repair
                else:
                    result = self.research.call(name, arguments)
                    if (result.get("review_scope") == "confirmed_leads"
                            and (repair := self._projection_repair(self.research._document()))):
                        result = repair
                    else:
                        saved = self._publish_confirmed()
                        if saved:
                            result["arena_checkpoint"] = saved
                            result["checkpoint_saved"] = saved["checkpoint_saved"]
                            if result.get("status") == "confirmed_leads_saved":
                                result["next"] = (
                                    "Confirmed leads are saved to /output/companies.json. Continue toward the original "
                                    "target; cost/time limits retain this partial list. Use tyche_finish to close a completed run."
                                )
            elif name == "tyche_open":
                validate(arguments, LAB_TOOLS[name][1])
                document = self.research._document()
                state = confirmed_leads.status(self.research.path, document)
                if ((state["pending_review"] or state["sync_required"])
                        and not self._accepted_source_url(arguments.get("url"))):
                    if repair := self._projection_repair(document):
                        result = repair
                    else:
                        self._publish_confirmed()
                        with self.research._review_lock:
                            result = self.research._confirm_leads()
                        if result.get("status") == "confirmed_leads_saved":
                            self._publish_confirmed()
                            result = self.public_web.open(**arguments)
                        else:
                            result["next"] = (
                                "Approve or correct the accepted evidence before opening a new URL. "
                                "Saved-source inspect and an exact URL already present in the pending accepted "
                                "evidence remain available for corroboration."
                            )
                else:
                    if not state["pending_review"] and not state["sync_required"]:
                        # A free unrelated read cannot bypass a failed host
                        # publication from an already confirmed native snapshot.
                        self._publish_confirmed()
                    result = self.public_web.open(**arguments)
            elif name == "tyche_finish":
                # Let native finish retain its blocker, stop and budget order;
                # only insert the Arena projection at its review boundary.
                self.research.review_delivery = self._review_delivery
                try:
                    result = self.research.call(name, arguments)
                finally:
                    self.research.review_delivery = self._native_review_delivery
            elif name == "tyche_lookup":
                # Retry a lost host acknowledgement before native confirmation
                # admits another paid lookup. Native TYCHE owns the pending set.
                self._publish_confirmed()
                document = self.research._document()
                state = confirmed_leads.status(self.research.path, document)
                if state["pending_review"] and (repair := self._projection_repair(document)):
                    result = repair
                else:
                    result = self.research.call(name, arguments)
            else:
                result = self.research.call(name, arguments)
            local_budget = self.broker.local_dispatch_budget()
            wrapped = (public_web_model_result(result, local_budget) if name == "tyche_open"
                       else lookup_model_result(result, local_budget) if name in {"tyche_lookup", "tyche_inspect"}
                       else model_result(result, local_budget))
            if (name == "tyche_inspect" and arguments.get("target") is not None
                    and arguments.get("field") == "evidence_review"
                    and wrapped.get("truncated") is True):
                wrapped = model_result(
                    evidence_review_page(result, arguments.get("offset", 0)),
                    local_budget,
                )
            return wrapped
        finally:
            self.lock.release()


def main():
    from .runtime import require_lab

    parent_pid = os.getppid()
    require_lab()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-file", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--response-deadline", type=float, required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    watcher = threading.Thread(target=watch_parent, args=(parent_pid, stopped), daemon=True)
    watcher.start()
    session = LabTools(args.run_file, args.deadline, args.response_deadline)
    try:
        # Already isolated by the lab. Do not use the local Codex sandbox relay.
        serve(session, tools=LAB_TOOLS)
    finally:
        session.broker.stopped.set()
        stopped.set()
        watcher.join(timeout=1)


if __name__ == "__main__":
    main()
