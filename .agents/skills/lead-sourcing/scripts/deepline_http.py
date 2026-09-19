"""One Deepline execution, retaining error bodies and request IDs for billing."""

import json
import os
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from provider_output import load_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never replay a POST or forward credentials to a redirect.


def execute(request):
    """No automatic retry: transport failures can have an unknown charge."""
    wire = Request(
        "https://code.deepline.com/api/v2/integrations/" + quote(request["tool"], safe="") + "/execute",
        data=json.dumps({"payload": request["payload"]}, allow_nan=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["DEEPLINE_API_KEY"].strip(),
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
