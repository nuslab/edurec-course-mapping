"""Keyed identifiers: the `request_id` and student pseudonym stored in place of the real identity.

Both are HMAC-SHA256 digests under the store's key, so they are stable across
exports yet cannot be reversed by hashing candidate student IDs without the key.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from dataclasses import replace

from .models import Identity, Request, plain


def keyed(key: bytes, value: str, length: int) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:length]


def request_id(key: bytes, identity: Identity) -> str:
    """The request's key: a digest of its identity in canonical JSON."""
    return keyed(key, json.dumps(plain(identity), sort_keys=True), 24)


def pseudonym(key: bytes, student_id: str) -> str:
    return f"student-{keyed(key, student_id, 12)}"


def pseudonymize(request: Request, key: bytes) -> Request:
    """A copy keyed by `request_id` with the student ID replaced; the original is untouched."""
    result = copy.deepcopy(request)
    result.request_id = request_id(key, request.identity)
    result.identity = replace(
        result.identity, student_id=pseudonym(key, request.identity.student_id)
    )
    return result
