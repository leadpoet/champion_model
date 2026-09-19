"""Credential-free Arena operation frames; no HTTP or direct-provider fallback."""

import base64
import copy
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from types import FunctionType
from urllib.parse import parse_qsl, urlencode, urlsplit

import budget_guard
import deepline
import scrapingdog
import scrapingdog_billing

PROVIDER_OVERHEAD_SECONDS = 65  # Arena admission 20 + billing 30 + API grace 15.
SCRAPINGDOG_PROVIDER_TIMEOUT_SECONDS = 60
DEEPLINE_PROVIDER_TIMEOUT_SECONDS = 240
PROVIDER_WAIT_SECONDS = PROVIDER_OVERHEAD_SECONDS + SCRAPINGDOG_PROVIDER_TIMEOUT_SECONDS
DEEPLINE_WAIT_SECONDS = PROVIDER_OVERHEAD_SECONDS + DEEPLINE_PROVIDER_TIMEOUT_SECONDS
PROVIDER_TIMEOUT_LIMITS = {
    "deepline": DEEPLINE_PROVIDER_TIMEOUT_SECONDS,
    "scrapingdog": SCRAPINGDOG_PROVIDER_TIMEOUT_SECONDS,
}
PROVIDERS = ("deepline", "scrapingdog")
SCRAPINGDOG_RUNTIME_HANDLE = "lab-arena-brokered-scrapingdog"
MODEL_PARTIAL_STOP_REASON = "preserve_reviewed_partial"

# These are the intersections between the native adapter and the Arena's
# existing closed operation table. Fields absent here are rejected before a
# budget reservation; they are never silently removed from a paid request.
_SCRAPINGDOG_ROUTES = {
    "google_search": ("/google", "scrapingdog.google", {"query", "country", "results"}),
    "scrape": ("/scrape", "scrapingdog.scrape", {"url", "dynamic", "premium", "wait"}),
    "linkedin_company": ("/profile", "scrapingdog.profile", {"type", "id"}),
    "linkedin_person": ("/profile", "scrapingdog.profile", {"type", "id"}),
    "linkedin_job": ("/jobs", "scrapingdog.jobs", {"job_id"}),
    "google_jobs": ("/google_jobs", "scrapingdog.google_jobs", {"query", "country"}),
    "google_news": ("/google_news", "scrapingdog.google_news", {"query", "country", "results"}),
    "linkedin_post": ("/profile/post", "scrapingdog.profile_post", {"id"}),
    "x_profile": ("/x/profile", "scrapingdog.x_profile", {"profileId"}),
    "x_post": ("/x/post", "scrapingdog.x_post", {"tweetId"}),
    "youtube_search": ("/youtube/search", "scrapingdog.youtube_search", {"search_query"}),
    "youtube_video": ("/youtube/video", "scrapingdog.youtube_video", {"v"}),
    "youtube_transcript": ("/youtube/transcripts", "scrapingdog.youtube_transcripts", {"v"}),
    "tiktok_profile": ("/tiktok/profile", "scrapingdog.tiktok_profile", {"username"}),
}
_SCRAPINGDOG_BOOL_FIELDS = {"dynamic", "premium"}
_SCRAPINGDOG_INT_FIELDS = {"wait"}
_GOOGLE_COUNTRIES = {"us", "gb", "ca", "au", "de", "fr", "nl", "ie", "in", "sg"}
_SCRAPINGDOG_FIELDS = {
    "scrapingdog.scrape": {"url": (str, 8, 2000), "dynamic": (bool, None, None),
                           "wait": (int, 0, 15000), "premium": (bool, None, None)},
    "scrapingdog.google": {"query": (str, 1, 500), "country": (str, 0, None)},
    "scrapingdog.google_news": {"query": (str, 1, 500), "country": (str, 0, None)},
    "scrapingdog.google_jobs": {"query": (str, 1, 500), "country": (str, 0, None)},
    "scrapingdog.x_post": {"tweetId": (str, 1, 64)},
    "scrapingdog.x_profile": {"profileId": (str, 1, 64)},
    "scrapingdog.profile": {"type": (str, 0, None), "id": (str, 1, 200)},
    "scrapingdog.profile_post": {"id": (str, 1, 200)},
    "scrapingdog.jobs": {"job_id": (str, 1, 64)},
    "scrapingdog.tiktok_profile": {"username": (str, 1, 64)},
    "scrapingdog.youtube_video": {"v": (str, 1, 32)},
    "scrapingdog.youtube_transcripts": {"v": (str, 1, 32)},
    "scrapingdog.youtube_search": {"search_query": (str, 1, 500)},
}
_SCRAPINGDOG_REQUIRED_FIELDS = {
    operation: set(fields) - ({"dynamic", "wait", "premium"} if operation == "scrapingdog.scrape" else
                              {"country"} if operation in {
                                  "scrapingdog.google", "scrapingdog.google_news", "scrapingdog.google_jobs"} else set())
    for operation, fields in _SCRAPINGDOG_FIELDS.items()
}
_SCRAPINGDOG_DEFAULT_CREDITS = 5
_SCRAPINGDOG_PROFILE_CREDITS = {"company": 10, "profile": 100}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class BrokerError(RuntimeError):
    pass


class BrokerRefusal(BrokerError):
    """A known refusal before provider dispatch, with its original code."""

    def __init__(self, code):
        self.code = code
        super().__init__("Arena refused operation: " + code)


def sourcing_cost_snapshot():
    """Expose the host's ICP total without reconstructing or adding local costs."""
    try:
        from lab_arena_checkpoint import (
            QuotaUnavailable, quota_usage, validate_quota_cost_snapshot,
        )
    except ImportError:
        return {"status": "unavailable"}
    try:
        snapshot = validate_quota_cost_snapshot(quota_usage(include_sourcing_cost=True))
    except QuotaUnavailable:
        return {"status": "unavailable"}
    return {
        "status": "available",
        "scope": "all_execute_attempts_for_this_icp",
        **snapshot["sourcing_cost"],
        "note": (
            "Includes OpenRouter and sourcing providers; excludes Arena verification. "
            "Do not add local provider costs to this total. Reservations are not settled spend. "
            "successful_microusd is confirmed successful settled spend and is the only sourcing "
            "amount used by the current Arena admission and eligibility contract. "
            "success_unresolved_microusd is a provisional upper bound for unresolved calls and can "
            "include their full reservations; it is not measured spend and does not block distinct "
            "work or final review. Let active calls finish and never replay the same uncertain call. "
            "Do not use unresolved amounts to infer cost eligibility or an automatic stop. Compare "
            "confirmed successful spend with "
            "per_qualified_pair_cap_microusd times the number of fully reviewed checkpoint pairs only "
            "as provisional guidance because Arena can reject a pair. "
            "The admission cap is not the final eligibility allowance. Preserve reviewed output "
            "immediately even while billing is pending; Arena resolves final cost eligibility."
        ),
    }


class Broker:
    def __init__(self, socket_path, deadline, *, response_deadline=None, catalog=None,
                 initial_calls=0, provider_blocked=False):
        if not Path(socket_path).is_absolute():
            raise ValueError("LAB_ARENA_WORKER_SOCKET must be an absolute path")
        self.socket_path = str(socket_path)
        self.deadline = deadline
        self.response_deadline = (deadline + DEEPLINE_WAIT_SECONDS
                                  if response_deadline is None else response_deadline)
        if self.response_deadline < self.deadline:
            raise ValueError("Arena response deadline cannot precede admission deadline")
        self.catalog = catalog if catalog is not None else json.loads(Path(__file__).with_name("catalog.json").read_text())["tools"]
        if type(initial_calls) is int:
            initial_calls = {"deepline": initial_calls, "scrapingdog": 0}
        if (not isinstance(initial_calls, dict) or set(initial_calls) != set(PROVIDERS)
                or any(type(value) is not int or value < 0 for value in initial_calls.values())):
            raise ValueError("Arena initial dispatch counts must be nonnegative integers by provider")
        if type(provider_blocked) is bool:
            provider_blocked = {"deepline": provider_blocked, "scrapingdog": False}
        if (not isinstance(provider_blocked, dict) or set(provider_blocked) != set(PROVIDERS)
                or any(type(value) is not bool for value in provider_blocked.values())):
            raise ValueError("Arena provider block states must be booleans by provider")
        self._calls = dict(initial_calls)
        self.lock = threading.Lock()
        # Arena admits one positive-cost provider call at a time for this ICP.
        # Match that boundary before creating the separate native reservation.
        self._paid_dispatch_lock = threading.Lock()
        self.stopped = threading.Event()
        self._provider_blocked = dict(provider_blocked)

    # Preserve the previous Deepline-only test and integration surface while
    # the model-visible budget below reports both provider counters.
    @property
    def calls(self):
        return self._calls["deepline"]

    @calls.setter
    def calls(self, value):
        self._calls["deepline"] = value

    @property
    def provider_blocked(self):
        return self._provider_blocked["deepline"]

    @provider_blocked.setter
    def provider_blocked(self, value):
        self._provider_blocked["deepline"] = value

    def provider_calls(self, provider):
        with self.lock:
            return self._calls[provider]

    def provider_is_blocked(self, provider):
        with self.lock:
            return self._provider_blocked[provider]

    def _block_provider(self, provider):
        with self.lock:
            self._provider_blocked[provider] = True

    def local_dispatch_budget(self):
        """Return adapter telemetry; the Arena host owns dispatch capacity."""
        with self.lock:
            used = dict(self._calls)
        return {
            "scope": "local_adapter_dispatch_counts",
            "providers": {provider: {"used": used[provider]} for provider in PROVIDERS},
            "authoritative_billing": False,
            "authoritative_sourcing_cost": sourcing_cost_snapshot(),
            "note": ("Local counts are telemetry without a capacity limit. The Arena host controls quota. "
                     "Use authoritative_sourcing_cost for host-reported spend when available."),
        }

    @staticmethod
    def _set_timeout(connection, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Arena provider response exceeded its wait limit")
        connection.settimeout(remaining)

    @staticmethod
    def _receive(connection, size, *, deadline=None):
        result = bytearray()
        while len(result) < size:
            if deadline is not None:
                Broker._set_timeout(connection, deadline)
            part = connection.recv(min(size - len(result), 65536))
            if not part:
                raise BrokerError("worker_unavailable: incomplete response; do not retry")
            result.extend(part)
        return bytes(result)

    def _admit(self, provider="deepline", *, allow_after_deadline=False):
        """Claim one local dispatch slot before the native budget is reserved."""

        if provider not in PROVIDERS:
            raise ValueError("Unsupported Arena provider")
        with self.lock:
            if self.stopped.is_set():
                raise BrokerRefusal("stopped")
            now = time.monotonic()
            if self.deadline - now <= 0 and not allow_after_deadline:
                raise BrokerRefusal("deadline_reached")
            if allow_after_deadline and self.response_deadline - now <= 0:
                raise BrokerRefusal("response_deadline_reached")
            if self._provider_blocked[provider]:
                raise BrokerRefusal(provider + "_blocked_after_uncertain_call")
            self._calls[provider] += 1

    def _release_admission(self, provider="deepline"):
        """Release only a slot proved not to have reached the Arena worker."""

        with self.lock:
            if self._calls[provider] <= 0:
                raise RuntimeError("Arena dispatch slot underflow")
            self._calls[provider] -= 1

    def _acquire_paid_dispatch(self, *, allow_after_deadline=False):
        """Wait interruptibly for this ICP's one positive-cost transport slot."""

        deadline = self.response_deadline if allow_after_deadline else self.deadline
        deadline_code = "response_deadline_reached" if allow_after_deadline else "deadline_reached"
        while True:
            if self.stopped.is_set():
                raise BrokerRefusal("stopped")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerRefusal(deadline_code)
            if self._paid_dispatch_lock.acquire(blocking=False):
                return
            self.stopped.wait(min(0.05, remaining))

    def _requires_paid_dispatch(self, request, provider):
        """Recognize only catalog-confirmed zero-cost calls as gate-free."""

        spend = request.get("spend", {})
        if not isinstance(spend, dict):
            return True  # The native guard rejects malformed spend before transport.
        if "max_cost_credits" in spend:
            try:
                if budget_guard.amount(spend["max_cost_credits"], "maximum call cost") != 0:
                    return True
            except (ValueError, TypeError, ArithmeticError):
                return True
        # Actual-cost ledgers omit a reservation bound. Absence does not make
        # the request free: only the bundled provider catalog can establish that.
        if provider != "deepline":
            return True
        tool = self.catalog.get(request.get("tool"))
        return not (
            isinstance(tool, dict)
            and isinstance(tool.get("pricing"), dict)
            and tool["pricing"].get("creditsPerUnit") == 0
        )

    def request(self, operation, parameters, *, admitted=False, timeout_seconds=None):
        provider = operation.split(".", 1)[0] if isinstance(operation, str) else None
        if provider not in PROVIDERS:
            raise ValueError("Unsupported Arena operation")
        if provider == "deepline" and operation != "deepline.execute":
            raise ValueError("Unsupported Arena operation")
        if operation == "deepline.execute" and parameters.get("tool") not in self.catalog:
            raise ValueError("Tool is absent from the bundled Arena catalog")
        if provider == "scrapingdog" and operation not in {
                route[1] for route in _SCRAPINGDOG_ROUTES.values()}:
            raise ValueError("Unsupported Arena operation")
        if (timeout_seconds is not None
                and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                     or not math.isfinite(timeout_seconds) or timeout_seconds <= 0)):
            raise ValueError("Arena operation timeout must be finite and positive")
        if self.response_deadline - time.monotonic() <= 0:
            raise BrokerRefusal("response_deadline_reached")
        claimed_admission = False
        if not admitted:
            self._admit(provider)
            claimed_admission = True
        remaining = self.response_deadline - time.monotonic()
        if remaining <= 0:
            if claimed_admission:
                self._release_admission(provider)
            raise BrokerRefusal("response_deadline_reached")
        provider_timeout = min(
            PROVIDER_TIMEOUT_LIMITS[provider],
            PROVIDER_TIMEOUT_LIMITS[provider] if timeout_seconds is None else timeout_seconds,
            remaining,
        )
        frame_timeout_ms = int(provider_timeout * 1000)
        frame = json.dumps({"schema_version": "leadpoet.lab_arena.operation_frame.v1",
            "operation_id": operation, "parameters": parameters,
            "timeout_ms": max(1, frame_timeout_ms)},
            allow_nan=False, separators=(",", ":")).encode()
        if len(frame) > 1048576:
            raise ValueError("Arena request exceeds frame limit")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                # The frame timeout still limits provider execution. A proved
                # pre-dispatch billing hold can outlast that window, so keep
                # this one socket alive only until the run's original absolute
                # response deadline. The worker retains the same action and
                # stops when this connection closes.
                wait_deadline = self.response_deadline
                self._set_timeout(connection, wait_deadline)
                connection.connect(self.socket_path)
                self._set_timeout(connection, wait_deadline)
                connection.sendall(len(frame).to_bytes(4, "big") + frame)
                size = int.from_bytes(self._receive(connection, 4, deadline=wait_deadline), "big")
                if not 2 <= size <= 4 * 1048576:
                    raise BrokerError("Invalid Arena response size")
                response = json.loads(self._receive(connection, size, deadline=wait_deadline))
        except (OSError, ValueError) as exc:
            # Dispatch may have billed. Never replay this request.
            raise BrokerError("Arena transport failed; do not retry the call") from exc
        if not isinstance(response, dict):
            raise BrokerError("Invalid Arena response envelope")
        if "error" in response:
            if set(response) == {"error"} and isinstance(response["error"], str) and response["error"] in {
                    "budget_exhausted", "invalid_frame", "frame_too_large", "invalid_request", "invalid_body"}:
                raise BrokerRefusal(response["error"])
            raise BrokerError("Arena refused operation: " + str(response["error"]))
        if set(response) != {"status", "headers", "body_b64"} or type(response["status"]) is not int:
            raise BrokerError("Invalid Arena response envelope")
        try:
            raw_body = base64.b64decode(response["body_b64"], validate=True)
            from provider_output import load_json
            body = load_json(raw_body) if provider == "deepline" else raw_body.decode("utf-8")
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            kind = "JSON" if provider == "deepline" else "text"
            raise BrokerError(f"Arena returned invalid provider {kind}; do not retry") from exc
        if not isinstance(response["headers"], dict):
            raise BrokerError("Invalid Arena response envelope")
        return response["status"], response["headers"], body

    @staticmethod
    def _valid_https_url(value):
        if (not isinstance(value, str) or not value.isascii() or any(char.isspace() for char in value)
                or "\\" in value or any(ord(char) < 0x21 or ord(char) == 0x7f for char in value)):
            return False
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            return False
        if (parts.scheme != "https" or parts.fragment or "#" in value or "@" in parts.netloc
                or not parts.hostname or port not in {None, 443}
                or parts.netloc.count(":") > 1 or parts.netloc.startswith("[")
                or parts.path and not parts.path.startswith("/")):
            return False
        host = parts.hostname.lower()
        labels = host.split(".")
        return (len(host) <= 253 and len(labels) >= 2 and labels[-1].isalpha()
                and all(_DNS_LABEL.fullmatch(label) for label in labels))

    @staticmethod
    def _validate_scrapingdog_parameters(operation_id, parameters):
        fields = _SCRAPINGDOG_FIELDS[operation_id]
        if set(parameters) - set(fields) or not _SCRAPINGDOG_REQUIRED_FIELDS[operation_id] <= set(parameters):
            raise scrapingdog.InputError("native ScrapingDog request does not match the Arena operation schema")
        for name, value in parameters.items():
            kind, minimum, maximum = fields[name]
            if type(value) is not kind:
                raise scrapingdog.InputError(f"{name} does not match the Arena operation schema")
            if kind is str and not (minimum <= len(value) and (maximum is None or len(value) <= maximum)):
                raise scrapingdog.InputError(f"{name} does not match the Arena operation bounds")
            if kind is int and not minimum <= value <= maximum:
                raise scrapingdog.InputError(f"{name} does not match the Arena operation bounds")
        if parameters.get("country") not in {None, *_GOOGLE_COUNTRIES}:
            raise scrapingdog.InputError("country is unavailable through the Arena operation")
        if operation_id == "scrapingdog.profile" and parameters["type"] not in {"company", "profile"}:
            raise scrapingdog.InputError("profile type does not match the Arena operation")
        if operation_id == "scrapingdog.scrape" and not Broker._valid_https_url(parameters["url"]):
            raise scrapingdog.InputError("scrape url must match the Arena HTTPS URL policy")

    @staticmethod
    def _scrapingdog_frame(request):
        """Match the native outbound request to one existing closed Arena operation."""

        operation_kind = request["operation_kind"]
        route = _SCRAPINGDOG_ROUTES.get(operation_kind)
        if route is None:
            supported = ", ".join(sorted(_SCRAPINGDOG_ROUTES))
            raise scrapingdog.InputError(
                f"{request['operation']} is unavailable through this Arena adapter; supported operations: {supported}")
        path, operation_id, allowed = route
        native_path, native_params = scrapingdog._params(request)
        if native_path != path:
            raise scrapingdog.InputError("native ScrapingDog route does not match the Arena operation")
        supplied = set(native_params) - {"api_key"}
        unsupported = sorted(supplied - allowed)
        if unsupported:
            raise scrapingdog.InputError(
                f"{request['operation']} options unavailable through Arena: {', '.join(unsupported)}")
        if operation_kind in {"google_search", "google_news"}:
            if str(native_params.get("results")) != "10":
                raise scrapingdog.InputError(
                    f"{request['operation']} requires the Arena-fixed results=10; omit custom results and use limit=10")
            supplied.remove("results")
        parameters = {key: native_params[key] for key in supplied}
        for key in _SCRAPINGDOG_BOOL_FIELDS & parameters.keys():
            if parameters[key] not in {"true", "false"}:
                raise scrapingdog.InputError(f"{key} must be boolean")
            parameters[key] = parameters[key] == "true"
        for key in _SCRAPINGDOG_INT_FIELDS & parameters.keys():
            try:
                parameters[key] = int(parameters[key])
            except (TypeError, ValueError, OverflowError) as exc:
                raise scrapingdog.InputError(f"{key} must be an integer") from exc
        Broker._validate_scrapingdog_parameters(operation_id, parameters)
        expected_url = scrapingdog.API_HOST + native_path + "?" + urlencode(native_params)
        parsed = urlsplit(expected_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) != (
                "https", "api.scrapingdog.com", path, "") or len(dict(pairs)) != len(pairs):
            raise scrapingdog.InputError("native ScrapingDog request does not match the closed Arena route")
        if dict(pairs).get("api_key") != SCRAPINGDOG_RUNTIME_HANDLE:
            raise scrapingdog.ConfigError("ScrapingDog Arena runtime handle is not configured")
        return expected_url, operation_id, parameters

    @staticmethod
    def _scrapingdog_minimum_credits(operation_id, parameters):
        if operation_id == "scrapingdog.profile":
            return _SCRAPINGDOG_PROFILE_CREDITS[parameters["type"]]
        return _SCRAPINGDOG_DEFAULT_CREDITS

    @staticmethod
    def _scrapingdog_runner(transport):
        """Bind transport per call without modifying the shared native module."""

        native = scrapingdog._run_validated
        globals_ = dict(native.__globals__)
        globals_["_http_get"] = transport
        return FunctionType(native.__code__, globals_, native.__name__, native.__defaults__, native.__closure__)

    @staticmethod
    def _scrapingdog_preflight_failure(request, capture, exc):
        """Finish an expected native no-send failure in the adapter response schema."""

        is_input = isinstance(exc, scrapingdog.InputError)
        status = "schema_error" if is_input else "config_error"
        error = {"message": scrapingdog.redact(str(exc))}
        capture({"arena": {"dispatched": False, "error": status}, "error": error})
        body = {"status": status, "provider": "scrapingdog",
                "operation": request.get("operation"), "error": error,
                "request_sent": False}
        if is_input:
            body["error_stage"] = "request"
        return body, 2

    def execute(self, request, capture, *, allow_after_deadline=False):
        operation = request["operation"]
        if operation in {"search", "describe"}:
            if operation == "describe":
                rows = [self.catalog[request["tool"]]] if request["tool"] in self.catalog else []
            else:
                words = request.get("query", "").casefold().split()
                scored = [(sum(word in json.dumps(row).casefold() for word in words), row)
                          for row in self.catalog.values()]
                rows = [row for score, row in sorted(scored, key=lambda item: -item[0])
                        if not words or score][:request.get("limit", 10)]
            return {"provider": "deepline", "operation": operation, "status": "ok" if rows else "no_results",
                    "results": copy.deepcopy(rows)}, 0
        is_deepline = operation == "execute"
        tariff = None
        if is_deepline and request.get("tool") not in self.catalog:
            raise ValueError("Only catalogued Arena Deepline operations are supported")
        if not is_deepline:
            try:
                request = scrapingdog.validate_request(request)
                handle = os.environ.get("SCRAPINGDOG_API_KEY")
                if handle != SCRAPINGDOG_RUNTIME_HANDLE:
                    raise scrapingdog.ConfigError("ScrapingDog Arena runtime handle is not configured")
                request["api_key"] = handle
                expected_url, operation_id, parameters = self._scrapingdog_frame(request)
                tariff = scrapingdog_billing.quote(request["operation_kind"], scrapingdog._params(request)[1])
                spend = request.get("spend")
                if isinstance(spend, dict) and "max_cost_credits" in spend:
                    try:
                        bound = budget_guard.amount(spend["max_cost_credits"], "ScrapingDog maximum call cost")
                    except (ValueError, TypeError, ArithmeticError):
                        pass  # The native guard returns its existing no-send refusal.
                    else:
                        minimum = self._scrapingdog_minimum_credits(operation_id, parameters)
                        if bound <= 0:
                            raise scrapingdog.InputError(
                                "ScrapingDog max_cost_credits must be strictly positive; no paid call was made")
                        if bound < minimum:
                            raise scrapingdog.InputError(
                                f"ScrapingDog max_cost_credits must cover the Arena operation cost of {minimum}; "
                                "no paid call was made")
            except (scrapingdog.InputError, scrapingdog.ConfigError) as exc:
                return self._scrapingdog_preflight_failure(request, capture, exc)
        provider = "deepline" if is_deepline else "scrapingdog"

        def refusal(exc, *, request_sent):
            status = ("quota_exceeded"
                      if "quota" in exc.code or exc.code == "budget_exhausted"
                      else "config_error")
            if exc.code in {"invalid_frame", "frame_too_large", "invalid_request", "invalid_body"}:
                status = "schema_error"
            if is_deepline:
                raw = {"body": {"status": status, "error": {"code": exc.code, "message": str(exc)}},
                       "exit_code": 2, "arena": {"dispatched": request_sent, "error": exc.code}}
                capture(raw)
                body, code = deepline.normalize_response(request, raw)
            else:
                raw = {"arena": {"dispatched": request_sent, "error": exc.code}}
                capture(raw)
                body, code = ({"status": status, "provider": "scrapingdog",
                               "operation": request["operation"]}, 2)
            body["request_sent"] = request_sent
            return body, code

        captured = {}
        original_capture = capture
        def save_response(raw):
            captured.update(raw)
            original_capture(raw)
        capture = save_response

        def dispatch():
            try:
                if is_deepline:
                    status, headers, payload = self.request("deepline.execute", {
                        "tool": request["tool"], "payload": request["payload"]}, admitted=True,
                        timeout_seconds=request["timeout_seconds"])
                else:
                    def transport(url, native_timeout_seconds):
                        if url != expected_url:
                            raise BrokerError("Native ScrapingDog request changed after admission; do not retry")
                        status, headers, payload = self.request(
                            operation_id, parameters, admitted=True, timeout_seconds=native_timeout_seconds)
                        return status, payload, None

                    return self._scrapingdog_runner(transport)(request, capture)
            except BrokerRefusal as exc:
                # A worker refusal is not a provider response or billing receipt.
                # Preserve its code and keep the conservative reservation.
                return refusal(exc, request_sent=True)
            except BrokerError as exc:
                # Retain the reservation and block further paid research when
                # the outcome is uncertain. No invented zero-cost receipt.
                self._block_provider(provider)
                raw = ({"body": {}, "timed_out": True, "stderr": str(exc)} if is_deepline else
                       {"timed_out": True, "incomplete": True, "arena": {"error": str(exc)}})
                capture(raw)
                if is_deepline:
                    return deepline.normalize_response(request, raw)
                return ({"status": "timeout", "provider": "scrapingdog",
                         "operation": request["operation"], "request_sent": True}, 0)
            raw = {"body": payload, "exit_code": 0 if 200 <= status < 300 else 2,
                   "stderr": "" if 200 <= status < 300 else f"Arena HTTP {status}",
                   "arena": {"status": status, "headers": headers}}
            capture(raw)
            return deepline.normalize_response(request, raw)

        paid_dispatch = self._requires_paid_dispatch(request, provider)
        paid_dispatch_acquired = False
        try:
            if paid_dispatch:
                self._acquire_paid_dispatch(
                    allow_after_deadline=allow_after_deadline
                )
                paid_dispatch_acquired = True
            try:
                self._admit(provider, allow_after_deadline=allow_after_deadline)
            except BrokerRefusal as exc:
                # No native reservation and no Arena frame exist for this refusal.
                return refusal(exc, request_sent=False)

            def accounted_dispatch():
                body, code = dispatch()
                if tariff:
                    body.update(tariff=tariff, **scrapingdog_billing.outcome(tariff, captured))
                return body, code
            body, code = budget_guard.guarded_call(request, provider, accounted_dispatch, tariff=tariff)
            if body.get("request_sent") is False:
                # The native ledger rejected before dispatch, so this local slot is
                # also unused. Never release after an Arena frame might have left.
                self._release_admission(provider)
            return body, code
        except BrokerRefusal as exc:
            # Waiting for the per-ICP transport never admits or reserves a call.
            return refusal(exc, request_sent=False)
        finally:
            if paid_dispatch_acquired:
                self._paid_dispatch_lock.release()
