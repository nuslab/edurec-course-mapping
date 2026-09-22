"""Stage 3: replace student identity in an export with per-run pseudonyms."""

from __future__ import annotations

import copy
import hashlib
import secrets
from pathlib import Path

from .models import Document


def anonymize(data: Document, salt: str | None = None) -> Document:
    """Return an anonymized deep copy; the original export is left untouched.

    Student IDs become `student-<hex>` pseudonyms that are consistent within the
    copy, so a student's requests can still be grouped, but the salt is random
    per run and never stored, so the pseudonyms cannot be reversed or matched
    across exports. Student names and EduRec user IDs are dropped.
    """
    salt = salt if salt is not None else secrets.token_hex(16)
    pseudonyms: dict[str, str] = {}

    def pseudonym(student_id: str) -> str:
        if student_id not in pseudonyms:
            digest = hashlib.sha256(f"{salt}:{student_id}".encode()).hexdigest()
            pseudonyms[student_id] = f"student-{digest[:12]}"
        return pseudonyms[student_id]

    result = copy.deepcopy(data)
    for request in result.requests:
        request.identity.student_id = pseudonym(request.identity.student_id)
    for page in result.list_pages:
        for row in page.rows:
            if row.student_id:
                row.student_id = pseudonym(row.student_id)
            row.student_name = None
            row.user_id = None
    result.collection.anonymized = True
    return result


def anonymized_path(output: str | Path) -> Path:
    """`module-mappings` -> `module-mappings-anonymized`."""
    path = Path(output)
    return path.with_name(f"{path.name}-anonymized")
