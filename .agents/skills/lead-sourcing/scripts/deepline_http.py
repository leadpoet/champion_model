"""One Deepline execution, retaining error bodies and request IDs for billing."""

import json
import os
import socket
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from provider_output import load_json

API_HOST = "https://code.deepline.com"


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
    return explicit or project_key.strip() or scoped.get("DEEPLINE_API_KEY", "").strip() or None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never replay a POST or forward credentials to a redirect.


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
    try:
        try:
            stream = build_opener(NoRedirect()).open(wire, timeout=request["timeout_seconds"])
        except HTTPError as exc:
            stream = exc  # An error response still carries authoritative IDs/billing.
        response["http_status"] = stream.code
        response["headers"] = {key: stream.headers[key] for key in
            ("x-deepline-request-id", "x-request-id", "x-vercel-id") if stream.headers.get(key)}
        raw = stream.read().decode("utf-8", errors="replace")
        try:
            response["body"] = load_json(raw)
        except ValueError:
            response["body"] = raw
        response["exit_code"] = 0 if 200 <= stream.code < 300 else 1
    except (TimeoutError, socket.timeout):
        response.update(timed_out=True)
    except (URLError, OSError, HTTPException):
        # Do not copy network exception text, which may contain credentials.
        response.update(exit_code=1, stderr="Deepline transport failed; execution and billing remain unknown")
    finally:
        if stream is not None:
            stream.close()
    return response
