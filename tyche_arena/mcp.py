"""Run-bound native TYCHE tools inside the lab's existing gVisor sandbox."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import threading

from .broker import Broker
from .output import deliver, projection_preflight
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
    tools["tyche_checkpoint"] = (
        "Save completed companies while research continues. Call after each accepted company; "
        "review the evidence packet, then approve its current review_ref. Only reviewed, fully "
        "qualified companies and contacts are checkpointed for the lab deadline. This does not "
        "end research or change the target; use tyche_finish to close the run.",
        {"type": "object", "properties": {"review_ref": {"type": "string", "minLength": 1}},
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
            "arena_budget": result.get("arena_budget"),
            "preview": encoded[:8000],
            "next": "Read narrower fields with tyche_inspect. For final review, inspect each accepted company's evidence_review before approving review_ref. This preview is incomplete."}


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

        def save(path, validation):
            result = deliver(path, validation, icp, lab_arena_checkpoint.write)
            self.delivered = True
            return result

        self.research = ResearchTools(run_file, execute=self.broker.execute, deliver=save)
        self._native_review_delivery = self.research.review_delivery

    def _review_delivery(self, document, review_ref=None):
        errors = projection_preflight(self.research.path, document, self.icp)
        if errors:
            return {"status": "needs_repair", "delivery_allowed": False, "errors": errors,
                    "next": "Correct the named Arena output fields with review/inspect before final evidence review. No approval or delivery occurred."}
        return self._native_review_delivery(document, review_ref)

    def checkpoint(self, review_ref=None):
        document = self.research._document()
        errors = (budget_guard.audit_ledger(self.research.path, document)
                  + projection_preflight(self.research.path, document, self.icp))
        if errors:
            return {"status": "needs_repair", "checkpoint_saved": False, "errors": errors}
        # Native research uses `0` to hand final review to a fresh context.
        # Checkpoints are an in-session partial save and retain their existing
        # evidence approval flow; do not turn them into final-review handoffs.
        phase = self.research.environment.get("TYCHE_FINALIZATION_ONLY")
        self.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        try:
            review = self.research.review_delivery(document, review_ref)
        finally:
            if phase is None:
                self.research.environment.pop("TYCHE_FINALIZATION_ONLY", None)
            else:
                self.research.environment["TYCHE_FINALIZATION_ONLY"] = phase
        if review:
            return review
        result = deliver(self.research.path, {"valid": True, "scope": "accepted_companies"},
                         self.icp, self.write_checkpoint, partial=True)
        return {**result, "status": "checkpoint_saved",
                "next": "Reviewed companies are saved. Continue research toward the original target, then tyche_finish."}

    def call(self, name, arguments):
        if name not in LAB_TOOLS:
            raise ValueError("The lab initialized this run; use its bound research tools")
        if name == "tyche_review" and "web" in arguments:
            raise ValueError("Lab evidence must come through the bound provider adapter")
        # Finish cannot race a review or a paid lookup. A lookup can still run
        # the normal three-check batch internally. Never alter delivered state.
        with self.lock:
            if self.delivered and (name != "tyche_inspect" or any(key in arguments for key in ("recover", "refresh", "query", "tool"))):
                raise ValueError("Reviewed JSON is delivered; end the Codex turn now")
            if name == "tyche_checkpoint":
                validate(arguments, LAB_TOOLS[name][1])
                result = self.checkpoint(**arguments)
            elif name == "tyche_finish":
                # Let native finish retain its blocker, stop and budget order;
                # only insert the Arena projection at its review boundary.
                self.research.review_delivery = self._review_delivery
                try:
                    result = self.research.call(name, arguments)
                finally:
                    self.research.review_delivery = self._native_review_delivery
            else:
                result = self.research.call(name, arguments)
            local_budget = self.broker.local_dispatch_budget()
            wrapped = model_result(result, local_budget)
            if (name == "tyche_inspect" and arguments.get("target") is not None
                    and arguments.get("field") == "evidence_review"
                    and wrapped.get("truncated") is True):
                wrapped = model_result(
                    evidence_review_page(result, arguments.get("offset", 0)),
                    local_budget,
                )
            return wrapped


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
