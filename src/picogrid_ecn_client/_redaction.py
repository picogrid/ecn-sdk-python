# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Secret-safe text handling used at every public error boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PEM = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    flags=re.DOTALL,
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+)[^\s,;]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(token|password|secret|client_key|private_key|authorization)\s*[:=]\s*([^\s,;&]+)"
)
_URL_USERINFO = re.compile(r"(https?://)([^/@\s]+)@")

REDACTED = "[REDACTED]"


def redact_text(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Return text with common credential forms and supplied values removed."""

    text = str(value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _PEM.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    return _URL_USERINFO.sub(lambda match: f"{match.group(1)}{REDACTED}@", text)
