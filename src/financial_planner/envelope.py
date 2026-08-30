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

# A credential is recognised by the SHAPE of its name on `config`, not by a
# hardcoded list of the two we happen to have today. A third key added to
# config.py used to leak until someone remembered to extend that list, which is
# exactly the kind of maintenance nobody remembers under a deadline.
#
# A bare ``_KEY`` was in this tuple and is not any more. It names an ordinary
# constant as readily as a credential: a ``PARTITION_KEY = "transaction_date"``
# added to config.py would have made `redact` replace that word everywhere it
# appeared -- in a spending breakdown's categories, in a document's schema
# listing, in an extracted PDF page -- and the model would read the mangled data
# with no error raised and no log line written. Discovery is only worth having
# while its false positives stay impossible; these three suffixes name nothing
# but a credential. A key called ``ENCRYPTION_KEY`` is not covered and has to be
# renamed to ``ENCRYPTION_API_KEY`` or ``ENCRYPTION_SECRET``, which is a visible
# step rather than a silent gap.
_SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")

# Below this length a value is not a credential, and replacing it would shred
# the message: a one-character secret matches between every character.
_MIN_SECRET_LENGTH = 8


def _configured_secrets() -> list[str]:
    """Return the secret values `config` currently holds, longest first.

    Both the names and the values are discovered at call time, not at import
    time: config reads its keys from the environment when it is imported, and
    both the app and the tests rebind (and add) those attributes afterwards.

    Longest first, so that when one secret is a substring of another the longer
    one is replaced whole rather than left as a redacted stub. That ordering is
    also what makes the output deterministic.
    """
    secrets = {
        value
        for name, value in vars(config).items()
        if name.endswith(_SECRET_NAME_SUFFIXES)
        and isinstance(value, str)
        and len(value) >= _MIN_SECRET_LENGTH
    }
    return sorted(secrets, key=lambda secret: (-len(secret), secret))


def redact(message: str) -> str:
    """Strip any configured secret out of text headed for the model.

    Upstream exception text is relayed verbatim so the model can act on it, and
    HTTP clients routinely quote the failing request back. Whatever this returns
    lands in the model's context and in the saved transcript, so a key echoed
    here outlives the error that produced it.

    Substring replacement rather than a pattern: the point is to catch the keys
    this process actually holds, not to guess at every credential format.
    """
    for secret in _configured_secrets():
        message = message.replace(secret, REDACTED)
    return message


def _redact_payload(value: Any) -> Any:
    """Redact the strings inside a payload, leaving its structure alone.

    This has to run *before* serialization. ``json.dumps`` defaults to
    ``ensure_ascii=True`` and escapes quotes and backslashes, so a key holding
    any of those appears in the serialized text in escaped form and a substring
    replacement over that text never finds it. `err` was unaffected because it
    redacts the raw message; only the success path serialized first.

    Keys are left alone deliberately: every payload here is built with literal
    keys by our own code, so a secret can only arrive in a value, and redacting
    keys could collapse two distinct ones into a single entry.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item) for item in value]
    return value


def ok(payload: dict[str, Any]) -> str:
    """Serialize a successful tool result.

    ``default=str`` covers the pandas and numpy scalars that reach here from the
    document and market tools; JSON has no opinion about a ``Period`` or an
    ``int64``.

    Success is redacted as well as failure. A key does not only leak through an
    exception: search snippets and other relayed upstream text ride back on the
    *successful* path too, and the redaction guarantee the README makes is over
    everything a tool returns, not just what it returns when it breaks.
    Redaction runs over the payload's strings *before* serialization, because
    `json.dumps` escapes non-ASCII, quotes and backslashes, and a replacement
    over the escaped text would walk straight past a key containing one. The
    serialized text is swept again afterwards as a backstop, since ``default=str``
    produces strings the first pass never saw. Neither pass touches the keys or
    the separators `_is_error_result` reads.
    """
    return redact(json.dumps(_redact_payload(payload), separators=(",", ":"), default=str))


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
