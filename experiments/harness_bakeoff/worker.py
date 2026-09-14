"""Fresh-process adapter entrypoint. Full ICP JSON arrives over stdin."""

from __future__ import annotations

import importlib
import json
import sys
import traceback
from typing import Any

from .models import validate_companies


MODULES = {
    "pydantic_ai": "harness",
}
SENTINEL = "BAKEOFF_RESULT_JSON="


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1 or args[0] not in MODULES:
        print(SENTINEL + json.dumps({"ok": False, "error": "invalid arm"}), flush=True)
        return 2
    arm = args[0]
    module: Any = None
    try:
        icp = json.load(sys.stdin)
        module = importlib.import_module(MODULES[arm])
        companies = validate_companies(
            module.run_icp(icp),
            allow_contacts=icp.get("contact_policy") == "contacts_v1",
            intent_details_policy=icp.get("intent_details_policy"),
        )
        usage_fn = getattr(module, "get_last_usage", None)
        if callable(usage_fn):
            usage: dict[str, Any] = usage_fn()
        else:
            raw_usage = getattr(module, "LAST_USAGE", {})
            usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
        print(
            SENTINEL + json.dumps({"ok": True, "companies": companies, "usage": usage}),
            flush=True,
        )
        return 0
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        usage: dict[str, Any] = {}
        if module is not None:
            usage_fn = getattr(module, "get_last_usage", None)
            if callable(usage_fn):
                raw_usage = usage_fn()
            else:
                raw_usage = getattr(module, "LAST_USAGE", {})
            if isinstance(raw_usage, dict):
                usage = dict(raw_usage)
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        print(
            SENTINEL
            + json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:2000]}",
                    "traceback": trace[-8000:],
                    "usage": usage,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
