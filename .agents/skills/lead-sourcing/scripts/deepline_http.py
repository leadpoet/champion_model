"""One Deepline execution, retaining error bodies and request IDs for billing."""

import io
import json
import os
import socket
import time
from pathlib import Path
from functools import partial
from http.client import HTTPException, HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from provider_output import load_json

API_HOST = "https://code.deepline.com"


class BillingUnavailable(ValueError):
    """A billing feed transport failed; another read-only feed may still work."""


def _transport_diagnostic(error, stage, started):
    # Exception messages may contain credentials. Types, numeric errno and our
    # own stage labels explain the failure without exposing their contents.
    cause = error.reason if isinstance(error, URLError) else error
    result = {"stage": stage, "error_type": type(error).__name__,
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    if isinstance(cause, BaseException):
        result["cause_type"] = type(cause).__name__
        if type(getattr(cause, "errno", None)) is int:
            result["errno"] = cause.errno
    return result


def _auth_file(path):
    """Read only the SDK's two auth fields; never evaluate shell syntax."""
    try:
        with path.open(encoding="utf-8") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(raw) > 65536:
        raise ValueError("Deepline auth configuration is too large")
    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() in {"DEEPLINE_HOST_URL", "DEEPLINE_API_KEY"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                fields[key.strip()] = value
    return fields


def api_key():
    """Use existing production CLI authentication without printing/copying it.

    Follow the SDK's env, nearest project, then host-scoped credential order.
    Custom CLI hosts/binaries retain their own authentication path.
    """
    explicit = os.environ.get("DEEPLINE_API_KEY", "").strip()
    if os.environ.get("DEEPLINE_BIN", "").strip() and not explicit:
        return None
    project = {}
    for directory in (Path.cwd(), *Path.cwd().parents):
        path = directory / ".env.deepline"
        if path.is_file():
            project = _auth_file(path)
            break
    scoped = _auth_file(Path.home() / ".local/deepline/code-deepline-com/.env")
    host = (os.environ.get("DEEPLINE_HOST_URL") or project.get("DEEPLINE_HOST_URL")
            or scoped.get("DEEPLINE_HOST_URL") or API_HOST).strip().rstrip("/")
    if host != API_HOST:
        return None  # Never forward another host's credentials to production.
    project_key = (project.get("DEEPLINE_API_KEY", "")
                   if project.get("DEEPLINE_HOST_URL", "").rstrip("/") == API_HOST else "")
    if explicit or project_key.strip():
        return explicit or project_key.strip()
    scoped_key = scoped.get("DEEPLINE_API_KEY", "").strip()
    if scoped_key and scoped.get("DEEPLINE_HOST_URL", API_HOST).strip().rstrip("/") != API_HOST:
        # Falling back to the CLI here could select the same mismatched key.
        raise ValueError("Deepline scoped credential belongs to another host")
    return scoped_key or None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never replay a POST or forward credentials to a redirect.


class _DeadlineReader(io.RawIOBase):
    """Apply the remaining response budget to every underlying socket read."""

    def __init__(self, raw, sock, deadline):
        self.raw, self.sock, self.deadline = raw, sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Deepline response deadline reached")
        self.sock.settimeout(remaining)
        return self.raw.readinto(buffer)

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineResponse(HTTPResponse):
    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        # Wrap before begin() reads headers. Buffering and chunk parsing still
        # belong to http.client; slow headers/chunks cannot reset the deadline.
        self.fp = io.BufferedReader(_DeadlineReader(self.fp.detach(), sock, deadline))


class _ResponseDeadline:
    def __init__(self, deadline, *args, **kwargs):
        self.deadline = deadline
        super().__init__(*args, **kwargs)

    def do_open(self, connection_class, request, **kwargs):
        def connection(*args, **options):
            result = connection_class(*args, **options)
            result.response_class = partial(_DeadlineResponse, deadline=self.deadline)
            return result
        # Preserve urllib's connection, proxy and TLS configuration unchanged.
        return super().do_open(connection, request, **kwargs)


class _DeadlineHTTPHandler(_ResponseDeadline, HTTPHandler):
    pass


class _DeadlineHTTPSHandler(_ResponseDeadline, HTTPSHandler):
    pass


def _opener(deadline):
    return build_opener(NoRedirect(), _DeadlineHTTPHandler(deadline), _DeadlineHTTPSHandler(deadline))


def billing_page(source, *, key, cursor=None, timeout=30):
    """Read billing with the same credentials/host as execution, never a POST.

    The usage feed groups charges. The credit ledger keeps individual posted
    debits; usage is still needed for free/failed attempts with no ledger debit.
    """
    if source not in {"ledger", "usage"}:
        raise ValueError("Unknown Deepline billing source")
    params = {"limit" if source == "ledger" else "recent_limit": "100"}
    if cursor:
        if source == "usage" and (not isinstance(cursor, str) or not cursor.isdecimal()):
            raise ValueError("Invalid Deepline usage offset")
        # The documented usage cursor repeated the same page in production.
        # Offsets advance correctly, including across grouped usage records.
        params["cursor" if source == "ledger" else "recent_offset"] = cursor
    wire = Request(API_HOST + "/api/v2/billing/" + source + "?" + urlencode(params),
                   headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with _opener(time.monotonic() + timeout).open(wire, timeout=timeout) as response:
            return load_json(response.read().decode("utf-8"), billing_feed=True)
    except (OSError, HTTPException):
        raise BillingUnavailable("Read-only Deepline billing feed unavailable; charges remain pending") from None
    except ValueError:
        # A malformed response is not an outage: fail closed, without its text.
        raise ValueError("Invalid Deepline billing response; charges remain pending") from None


def execute(request, key=None):
    """No automatic retry: transport failures can have an unknown charge."""
    wire = Request(
        API_HOST + "/api/v2/integrations/" + quote(request["tool"], safe="") + "/execute",
        data=json.dumps({"payload": request["payload"]}, allow_nan=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + (key or os.environ["DEEPLINE_API_KEY"].strip()),
                 "Content-Type": "application/json", "Accept": "application/json",
                 "X-Deepline-Tool-Error-Schema": "1",
                 "X-Deepline-Execute-Response-Contract": "raw-v2",
                 "X-Deepline-Execute-Response-Intent": "raw"},
        method="POST",
    )
    response = {"body": "", "stderr": "", "headers": {}}
    stream = None
    started, stage = time.monotonic(), "opening_response"
    try:
        try:
            stream = _opener(started + request["timeout_seconds"]).open(wire, timeout=request["timeout_seconds"])
        except HTTPError as exc:
            stream = exc  # An error response still carries authoritative IDs/billing.
        response["http_status"] = stream.code
        response["headers"] = {key: stream.headers[key] for key in
            ("x-deepline-request-id", "x-request-id", "x-vercel-id") if stream.headers.get(key)}
        stage = "reading_response"
        raw = stream.read().decode("utf-8", errors="replace")
        try:
            response["body"] = load_json(raw)
        except ValueError:
            response["body"] = raw
        response["exit_code"] = 0 if 200 <= stream.code < 300 else 1
    except (TimeoutError, socket.timeout) as exc:
        response.update(timed_out=True, transport=_transport_diagnostic(exc, stage, started))
    except (URLError, OSError, HTTPException) as exc:
        # Do not copy network exception text, which may contain credentials.
        response.update(exit_code=1, stderr="Deepline transport failed; execution and billing remain unknown",
                        transport=_transport_diagnostic(exc, stage, started))
        if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError):
            response["timed_out"] = True  # urllib wraps timeouts while reading headers.
    finally:
        if stream is not None:
            stream.close()
    return response
