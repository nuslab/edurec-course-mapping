"""Keyed identifiers: the `request_id` and student pseudonym stored in place of the real identity.

Both are HMAC-SHA256 digests under the store's secret, so they are stable across
exports yet cannot be reversed by hashing candidate student IDs without the secret.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from dataclasses import replace

from .models import Identity, Request, plain


def keyed(secret: bytes, value: str, length: int) -> str:
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:length]


def request_id(secret: bytes, identity: Identity) -> str:
    """The request's key: a digest of its seven identity values in canonical JSON."""
    return keyed(secret, json.dumps(plain(identity), sort_keys=True), 24)


def pseudonym(secret: bytes, student_id: str) -> str:
    return f"student-{keyed(secret, student_id, 12)}"


def anonymize(request: Request, secret: bytes) -> Request:
    """A copy keyed by `request_id` with the student ID replaced; the original is untouched."""
    result = copy.deepcopy(request)
    result.request_id = request_id(secret, request.identity)
    result.identity = replace(
        result.identity, student_id=pseudonym(secret, request.identity.student_id)
    )
    return result
