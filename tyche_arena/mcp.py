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
    tools["tyche_lookup"][1]["properties"]["checks"]["items"]["properties"]["provider"]["enum"] = ["deepline"]
    del tools["tyche_review"][1]["properties"]["web"]
    tools["tyche_checkpoint"] = (
        "Compatibility checkpoint tool. Normally tyche_review approval saves automatically. "
        "Review the evidence packet, then approve its current review_ref. Only reviewed, fully "
        "qualified companies and contacts are checkpointed for the lab deadline. This does not "
        "end research or change the target; use tyche_finish to close the run.",
        {"type": "object", "properties": {"review_ref": {"type": "string", "minLength": 1}},
         "additionalProperties": False})
    return {name: (description, arena_schema(schema)) for name, (description, schema) in tools.items()}


LAB_TOOLS = lab_tools()
MODEL_RESULT_MAX_CHARACTERS = 24000
EVIDENCE_REVIEW_PAGE_CHARACTERS = 8000


def watch_parent(parent_pid, stopped):
    """Codex launches MCP in its own process group; follow its lifetime too."""
    while not stopped.wait(0.25):
        if parent_pid <= 1 or os.getppid() != parent_pid:
            # An interrupted call retains its saved reservation. Never let an
            # orphan keep spending or publish a late checkpoint after Codex dies.
            os._exit(1)


def model_result(result):
    encoded = json.dumps(result, ensure_ascii=True)
    if len(encoded) <= MODEL_RESULT_MAX_CHARACTERS:
        return result
    return {"truncated": True, "status": result.get("status"), "review_ref": result.get("review_ref"),
            "review_scope": result.get("review_scope"), "confirmed_leads": result.get("confirmed_leads"),
            "arena_checkpoint": result.get("arena_checkpoint"),
            "preview": encoded[:8000],
            "next": "Read narrower fields with tyche_inspect. Inspect each listed company's evidence_review before returning review_ref to the requesting tool. This preview is incomplete."}


def evidence_review_page(result, offset):
    """Page the complete review without changing its evidence or approval."""
    content = json.dumps(result, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":"), sort_keys=True)
    next_offset = min(len(content), offset + EVIDENCE_REVIEW_PAGE_CHARACTERS)
    return {"status": "evidence_review_page", "content": content[offset:next_offset],
            "content_sha256": hashlib.sha256(content.encode("ascii")).hexdigest(),
            "total_characters": len(content), "offset": offset,
            "next_offset": next_offset if next_offset < len(content) else None,
            "encoding": "JSON with ensure_ascii=true, sorted keys, and compact separators",
            "next": "Request each page with next_offset. Require identical content_sha256 and "
                    "total_characters on every page; restart at offset 0 if either changes. "
                    "Concatenate content in offset order, parse the JSON and read all pages "
                    "before approving review_ref. Paging does not approve review_ref."}


class LabTools:
    def __init__(self, run_file, deadline):
        import lab_arena_checkpoint

        self.broker = Broker(os.environ["LAB_ARENA_WORKER_SOCKET"], deadline)
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

    def _projection_error(self, document):
        errors = projection_preflight(self.research.path, document, self.icp)
        if errors:
            return {"status": "needs_repair", "delivery_allowed": False, "errors": errors,
                    "next": "Correct the Arena output fields with review/inspect, then request evidence review again."}

    def _review_delivery(self, document, review_ref=None):
        return self._projection_error(document) or self._native_review_delivery(document, review_ref)

    def checkpoint(self, review_ref=None):
        document = self.research._document()
        errors = (budget_guard.audit_ledger(self.research.path, document)
                  + projection_preflight(self.research.path, document, self.icp))
        if errors:
            return {"status": "needs_repair", "checkpoint_saved": False, "errors": errors}
        # A checkpoint reviews a partial snapshot in this session. Preserve the
        # native final-review handoff for finish, without handing off research
        # each time a company is checkpointed.
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
                return model_result(self.checkpoint(**arguments))
            if name == "tyche_lookup":
                # Retry a failed publication before admitting more research.
                publish_confirmed(self.research.path, self.icp, self.write_checkpoint, os.environ["LAB_ARENA_OUTPUT_PATH"])
            if name == "tyche_review" and "review_ref" in arguments:
                # Incremental approval also publishes immediately. Check the
                # output contract before the native confirmation is persisted.
                validate(arguments, LAB_TOOLS[name][1])
                if error := self._projection_error(self.research._document()):
                    return model_result(error)
            if name == "tyche_finish":
                # Preserve native stop, budget and source gates before checking
                # the Arena projection at the final-review boundary.
                self.research.review_delivery = self._review_delivery
                try:
                    return model_result(self.research.call(name, arguments))
                finally:
                    self.research.review_delivery = self._native_review_delivery
            result = self.research.call(name, arguments)
            if result.get("status") == "review_required" and result.get("review_scope") == "confirmed_leads":
                result = self._projection_error(self.research._document()) or result
            if name == "tyche_review":
                saved = publish_confirmed(self.research.path, self.icp, self.write_checkpoint, os.environ["LAB_ARENA_OUTPUT_PATH"])
                if saved:
                    result["arena_checkpoint"] = saved
                    if result.get("status") == "confirmed_leads_saved":
                        result["next"] = "Confirmed leads are saved to /output/companies.json. Continue toward the original target; cost/time limits retain this partial list. Use tyche_finish to close a completed run."
            wrapped = model_result(result)
            if (name == "tyche_inspect" and arguments.get("target") is not None
                    and arguments.get("field") == "evidence_review"
                    and wrapped.get("truncated") is True):
                return model_result(evidence_review_page(result, arguments.get("offset", 0)))
            return wrapped


def main():
    from .runtime import require_lab

    parent_pid = os.getppid()
    require_lab()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-file", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    watcher = threading.Thread(target=watch_parent, args=(parent_pid, stopped), daemon=True)
    watcher.start()
    session = LabTools(args.run_file, args.deadline)
    try:
        # Already isolated by the lab. Do not use the local Codex sandbox relay.
        serve(session, tools=LAB_TOOLS)
    finally:
        session.broker.stopped.set()
        stopped.set()
        watcher.join(timeout=1)


if __name__ == "__main__":
    main()
