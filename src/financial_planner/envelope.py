"""The JSON envelope every tool returns, and the redaction it carries.

Tools return errors instead of raising, so that a bad argument lets the model
read the problem and retry rather than ending the turn. That makes the envelope
a contract with two consumers that must not drift apart:

* :func:`financial_planner.streaming._is_error_result` decides whether the UI
  reports a call as failed, and it does so by matching the *serialized* text
  against ``{"error"``. Compact separators and "error" first are load-bearing,
  not style.
* The model reads the message and acts on it, so it has to say what went wrong
  specifically enough to correct.

Four copies of these two functions had already diverged -- one omitted the
exception type, and only one of them redacted secrets. Since the failure that
motivates redaction is a *relayed upstream* message, and upstream messages
arrive through whichever tool happens to be calling out, redaction belongs in
the shared envelope rather than at one call site.
"""

from __future__ import annotations

import json
from typing import Any

# Imported as a module, not by value: config reads its keys from the
# environment at import time, and both the app and the tests rebind those
# attributes afterwards. A by-value import here would redact yesterday's key.
from financial_planner import config

REDACTED = "***"

# Attributes on `config` holding a secret. Read by name at call time so a
# credential that changes mid-session is still caught.
_SECRET_ATTRS = ("ANTHROPIC_API_KEY", "TAVILY_API_KEY")


def redact(message: str) -> str:
    """Strip any configured secret out of text headed for the model.

    Upstream exception text is relayed verbatim so the model can act on it, and
    HTTP clients routinely quote the failing request back. Whatever this returns
    lands in the model's context and in the saved transcript, so a key echoed
    here outlives the error that produced it.

    Substring replacement rather than a pattern: the point is to catch the keys
    this process actually holds, not to guess at every credential format.
    """
    for attr in _SECRET_ATTRS:
        secret = getattr(config, attr, None)
        # Guard the length: an empty or one-character value would otherwise
        # replace between every character of the message.
        if secret and len(secret) >= 8:
            message = message.replace(secret, REDACTED)
    return message


def ok(payload: dict[str, Any]) -> str:
    """Serialize a successful tool result.

    ``default=str`` covers the pandas and numpy scalars that reach here from the
    document and market tools; JSON has no opinion about a ``Period`` or an
    ``int64``.
    """
    return json.dumps(payload, separators=(",", ":"), default=str)


def err(problem: Exception | str) -> str:
    """Serialize a tool failure the model can read and retry from.

    The exception type is included because it is often the only thing
    distinguishing "you passed the wrong column" from "this file is not
    readable", and the model chooses a different recovery for each.
    """
    message = (
        f"{type(problem).__name__}: {problem}" if isinstance(problem, Exception) else str(problem)
    )
    return json.dumps({"error": redact(message)}, separators=(",", ":"))
