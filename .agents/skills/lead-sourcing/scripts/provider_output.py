"""Preserve a provider response before normalization, without repeating a call."""

import json
from decimal import Decimal
import math
import os
from pathlib import Path
import tempfile


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def load_json(text, *, billing_feed=False):
    # Preserve monetary decimals that cannot round-trip through a JSON float.
    # Company data keeps its existing numeric types; canonical ledger amounts
    # and exceptional-precision billing fields use decimal strings.
    money = {"credits_charged", "cost_usd", "credits", "delta", "charge_credits", "postedCredits"}
    def values(value, key=None, in_billing=False):
        if isinstance(value, Decimal):
            number = _finite_float(str(value))
            return str(value) if in_billing and key in money and Decimal(str(number)) != value else number
        if isinstance(value, dict):
            return {name: values(item, name, in_billing or name == "billing") for name, item in value.items()}
        if isinstance(value, list):
            return [values(item, in_billing=in_billing) for item in value]
        return value
    return values(json.loads(text, parse_float=Decimal, parse_constant=_finite_float), in_billing=billing_feed)


def response_body(parsed, text):
    """Keep diagnostic text when the provider response could not be parsed."""
    return text if parsed is None else parsed


class ResponseFile:
    def __init__(self, path, redact, metadata=None):
        self.path = Path(path)
        self.redact = redact
        self.response = None
        self.metadata = dict(metadata or {})
        # Reserve a unique destination and prove it is writable before dispatch.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self.identity = self._identity()
        self._write({"receipt_status": "pending"})

    def _identity(self):
        stat = self.path.lstat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _write(self, document):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(self.redact(dict(document, **self.metadata)), stream, ensure_ascii=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self._identity() != self.identity:
                raise OSError("response output changed during the request")
            os.replace(temporary, self.path)
            self.identity = self._identity()
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def capture(self, response):
        self.response = self.redact(response)
        try:
            self._write({"receipt_status": "response_received", "provider_response": self.response})
        except (OSError, TypeError, ValueError):
            # Keep the response in memory for the final save; never retry the provider.
            pass

    def finish(self, body):
        document = dict(body, receipt_status="complete")
        if self.response is not None:
            document["provider_response"] = self.response
        try:
            self._write(document)
        except (OSError, TypeError, ValueError):
            return False
        return True
