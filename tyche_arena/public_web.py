"""Bounded public-page reads through the Arena host's loopback proxy."""

from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib import error, parse, request

import budget_guard
import run_attempt as runner
from source_receipts import read_receipt, request_fingerprint
from .output import public_url


PROXY_ENV = "LAB_ARENA_WEB_PROXY_URL"
TOTAL_TIMEOUT_SECONDS = 20.0
MAX_RAW_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 64 * 1024
PREVIEW_CHARACTERS = 8000
READ_BYTES = 64 * 1024
PHASE_REQUEST_FIELD = "arena_review_phase"
TARGET_REQUEST_FIELD = "arena_target_scope"
FETCH_SCHEMA_ERRORS = {
    "unsupported_content_type", "unsupported_content_encoding", "unsupported_charset",
    "invalid_html", "no_readable_text",
}


class _HTTPStatusError(OSError):
    """Carry only a validated HTTP status across the isolated child boundary."""

    def __init__(self, status):
        super().__init__("http_status")
        self.status = _http_status(status)
        if self.status is None:
            raise ValueError("invalid_http_status")


def _http_status(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 300 <= value <= 599:
        return None
    return value


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self.parts = []

    def handle_starttag(self, tag, _attrs):
        if tag.casefold() in {"script", "style", "noscript", "template"}:
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript", "template"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("tyche_open url must be a bounded HTTP(S) URL")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("tyche_open url must be a bounded HTTP(S) URL")
    try:
        parsed = parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("tyche_open url must be a bounded HTTP(S) URL") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or port is not None and not 0 < port < 65536):
        raise ValueError("tyche_open url must be a bounded HTTP(S) URL")
    try:
        public_url(value)
    except ValueError:
        raise ValueError("tyche_open url must be a public HTTP(S) URL") from None
    return value


def _proxy(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Arena public-web proxy is unavailable")
    try:
        parsed = parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Arena public-web proxy is unavailable") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Arena public-web proxy is unavailable")
    return value


def _phase(environment):
    return "finalization" if environment.get("TYCHE_FINALIZATION_ONLY") == "1" else "research"


def _normalize(raw, content_type):
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if not (media_type.startswith("text/") or media_type in {
            "application/json", "application/xhtml+xml", "application/xml"}):
        raise ValueError("unsupported_content_type")
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, separator, value = part.strip().partition("=")
        if separator and key.casefold() == "charset" and value.strip(' \"'):
            charset = value.strip(' \"')
            break
    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        raise ValueError("unsupported_charset") from None
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText()
        try:
            parser.feed(decoded)
            parser.close()
        except Exception:
            raise ValueError("invalid_html") from None
        decoded = "\n".join(parser.parts)
    return " ".join(decoded.replace("\x00", " ").split())


def _response_socket(response):
    candidates = [getattr(response, "fp", None)]
    for _ in range(3):
        candidates += [getattr(value, name, None) for value in tuple(candidates) if value is not None
                       for name in ("raw", "_sock")]
    return next((value for value in candidates if callable(getattr(value, "settimeout", None))), None)


def _fetch(url, proxy_url, deadline, *, clock=time.monotonic):
    opener = request.build_opener(
        request.ProxyHandler({"http": proxy_url, "https": proxy_url}), _NoRedirect())
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("deadline")
    req = request.Request(url, method="GET", headers={
        "Accept": "text/html,text/plain,application/xhtml+xml,application/json;q=0.8",
        "Accept-Encoding": "identity",
        "User-Agent": "Leadpoet-Tyche-Arena/1.0",
    })
    with opener.open(req, timeout=remaining) as response:
        if response.status != 200:
            raise OSError("http_status")
        if response.headers.get("Content-Encoding", "").casefold() not in {"", "identity"}:
            raise ValueError("unsupported_content_encoding")
        sock = _response_socket(response)
        raw = bytearray()
        raw_truncated = False
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("deadline")
            if sock is not None:
                try:
                    sock.settimeout(remaining)
                except OSError:
                    pass  # The parent child-process timeout is the hard wall bound.
            part = response.read(min(READ_BYTES, MAX_RAW_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
            if len(raw) > MAX_RAW_BYTES:
                del raw[MAX_RAW_BYTES:]
                raw_truncated = True
                break
        content_type = response.headers.get("Content-Type", "")
    text = _normalize(bytes(raw), content_type)
    if not text:
        raise ValueError("no_readable_text")
    observed_characters = len(text)
    text_truncated = observed_characters > MAX_TEXT_CHARACTERS
    text = text[:MAX_TEXT_CHARACTERS]
    return {
        "url": url,
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "saved_characters": len(text),
        "observed_characters": observed_characters,
        "raw_bytes_observed": len(raw),
        "raw_truncated": raw_truncated,
        "text_truncated": text_truncated,
        "truncated": raw_truncated or text_truncated,
        "capture": "arena_public_web_proxy",
    }


def _fetch_isolated(url, proxy_url, deadline, *, clock=time.monotonic):
    """Enforce one wall deadline across DNS/proxy headers and every body read."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("deadline")
    environment = {
        "LAB_ARENA_WEB_PROXY_URL": proxy_url,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LC_ALL": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "tyche_arena.public_web", "--fetch-child"],
            input=json.dumps({"url": url, "timeout": remaining}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
            timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("deadline") from None
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 400_000:
        raise OSError("fetch_child_failed")
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError):
        raise OSError("fetch_child_failed") from None
    if not isinstance(result, dict) or set(result) != {"status", "value"}:
        raise OSError("fetch_child_failed")
    if result["status"] == "ok" and isinstance(result["value"], dict):
        return result["value"]
    if result["status"] == "timeout":
        raise TimeoutError("deadline")
    if (result["status"] == "schema_error" and isinstance(result["value"], str)
            and result["value"] in FETCH_SCHEMA_ERRORS):
        raise ValueError(result["value"])
    if result["status"] == "provider_error" and isinstance(result["value"], dict):
        value = result["value"]
        status = _http_status(value.get("http_status"))
        if set(value) == {"error", "http_status"} and value.get("error") == "http_error" and status:
            raise _HTTPStatusError(status)
    raise OSError("fetch_child_failed")


class PublicWeb:
    """Plan with native TYCHE, then save one bounded host-proxied observation."""

    def __init__(self, research, response_deadline, *, environment=None, clock=None):
        self.research = research
        self.response_deadline = response_deadline
        self.environment = os.environ if environment is None else environment
        self.clock = time.monotonic if clock is None else clock

    @staticmethod
    def _request(url, phase, target):
        return {"operation": "open", "query": url, PHASE_REQUEST_FIELD: phase,
                TARGET_REQUEST_FIELD: target.casefold().removeprefix("www.")}

    def _saved(self, target, fingerprint):
        document = budget_guard.read_object(Path(self.research.path))
        routes = document.get("routes", []) + document.get("stop_audit", {}).get("route_frontier", [])
        matches = [route for route in routes if isinstance(route, dict)
                   and route.get("provider") == "public_web"
                   and route.get("scope") == target.casefold().removeprefix("www.")
                   and route.get("request_fingerprint") == fingerprint]
        if not matches:
            return None
        route_id = matches[-1].get("route_id")
        saved = read_receipt(self.research.path, route_id)["result"]
        if saved.get("receipt_status") != "complete":
            return {"status": "pending", "ref": route_id,
                    "next": "The saved public-page plan has no complete observation. Do not fetch it again."}
        return self._view(route_id, saved, cached=True)

    @staticmethod
    def _view(route_id, saved, *, cached=False):
        rows = saved.get("results", [])
        row = rows[0] if rows and isinstance(rows[0], dict) else None
        result = {"status": saved.get("status"), "ref": route_id,
                  "cached": cached, "results": len(rows)}
        if row is None:
            result["error"] = saved.get("error", "arena_public_web_failed")
            return result
        ref = route_id + ":0"
        text = row.get("text", "")
        next_offset = PREVIEW_CHARACTERS if len(text) > PREVIEW_CHARACTERS else None
        result.update({
            "ref": ref,
            "url": row.get("url"),
            "text": text[:PREVIEW_CHARACTERS],
            "content_sha256": row.get("content_sha256"),
            "saved_characters": row.get("saved_characters"),
            "observed_characters": row.get("observed_characters"),
            "truncated": row.get("truncated", False),
            "next_offset": next_offset,
            "next": (None if next_offset is None else
                     {"tool": "tyche_inspect", "arguments": {
                         "ref": ref, "field": "text", "offset": next_offset}}),
        })
        return result

    def open(self, target, purpose, url):
        # Validate every local capability before native planning mutates the run.
        if not isinstance(target, str) or not target.strip() or len(target) > 253:
            raise ValueError("tyche_open target must be a bounded company scope")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 500:
            raise ValueError("tyche_open purpose must be bounded text")
        target = target.strip().casefold().removeprefix("www.")
        purpose = purpose.strip()
        url = _url(url)
        proxy_url = _proxy(self.environment.get(PROXY_ENV))
        phase = _phase(self.environment)
        native_request = self._request(url, phase, target)
        fingerprint = request_fingerprint("public_web", native_request)
        if saved := self._saved(target, fingerprint):
            return saved
        if self.response_deadline <= self.clock():
            raise ValueError("Arena response deadline has passed")

        lookup = {"provider": "public_web", "scope": target,
                  "phase": "account_verification", "purpose": purpose,
                  "request": native_request}
        planned = runner.run_lookup(self.research.path, lookup, plan_only=True)
        route_id = planned["attempt"]["action"]["id"]
        deadline = min(self.clock() + TOTAL_TIMEOUT_SECONDS, self.response_deadline)
        try:
            row = _fetch_isolated(url, proxy_url, deadline, clock=self.clock)
            response = {"status": "partial" if row["truncated"] else "ok",
                        "operation": "open", "results": [row]}
        except (TimeoutError, socket.timeout):
            response = {"status": "timeout", "operation": "open", "results": [],
                        "error": "arena_public_web_timeout"}
        except ValueError as exc:
            response = {"status": "schema_error", "operation": "open", "results": [],
                        "error": ("arena_public_web_" + str(exc) if str(exc) in FETCH_SCHEMA_ERRORS
                                  else "arena_public_web_invalid_response")}
        except _HTTPStatusError as exc:
            response = {"status": "provider_error", "operation": "open", "results": [],
                        "error": "arena_public_web_http_" + str(exc.status)}
        except (error.URLError, error.HTTPError, OSError):
            response = {"status": "provider_error", "operation": "open", "results": [],
                        "error": "arena_public_web_fetch_failed"}
        runner.complete_public_web(self.research.path, route_id, response, check_stop=False)
        saved = read_receipt(self.research.path, route_id)["result"]
        return self._view(route_id, saved, cached=False)


def _child_main():
    try:
        payload = json.loads(sys.stdin.read(8193))
        if (not isinstance(payload, dict) or set(payload) != {"url", "timeout"}
                or isinstance(payload["timeout"], bool)
                or not isinstance(payload["timeout"], (int, float))
                or not 0 < payload["timeout"] <= TOTAL_TIMEOUT_SECONDS):
            raise ValueError("invalid_child_request")
        url = _url(payload["url"])
        proxy_url = _proxy(os.environ.get(PROXY_ENV))
        row = _fetch(url, proxy_url, time.monotonic() + payload["timeout"])
        result = {"status": "ok", "value": row}
    except (TimeoutError, socket.timeout):
        result = {"status": "timeout", "value": "arena_public_web_timeout"}
    except ValueError as exc:
        result = {"status": "schema_error", "value": str(exc)}
    except error.HTTPError as exc:
        status = _http_status(exc.code)
        result = ({"status": "provider_error",
                   "value": {"error": "http_error", "http_status": status}}
                  if status is not None else
                  {"status": "provider_error", "value": "arena_public_web_fetch_failed"})
    except (error.URLError, OSError):
        result = {"status": "provider_error", "value": "arena_public_web_fetch_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


if __name__ == "__main__" and sys.argv[1:] == ["--fetch-child"]:
    _child_main()
